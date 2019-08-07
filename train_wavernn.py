import time
import numpy as np
import torch
from torch import optim
import torch.nn.functional as F
from utils.display import stream, simple_table
from utils.dataset import get_vocoder_datasets
from utils.distribution import discretized_mix_logistic_loss
from utils import hparams as hp
from models.fatchord_version import WaveRNN
from gen_wavernn import gen_testset
from utils.paths import Paths
import argparse
from utils import data_parallel_workaround


def main():

    # Parse Arguments
    parser = argparse.ArgumentParser(description='Train WaveRNN Vocoder')
    parser.add_argument('--lr', '-l', type=float,  help='[float] override hparams.py learning rate')
    parser.add_argument('--batch_size', '-b', type=int, help='[int] override hparams.py batch size')
    parser.add_argument('--force_train', '-f', action='store_true', help='Forces the model to train past total steps')
    parser.add_argument('--gta', '-g', action='store_true', help='train wavernn on GTA features')
    parser.add_argument('--force_cpu', '-c', action='store_true', help='Forces CPU-only training, even when in CUDA capable environment')
    parser.add_argument('--hp_file', metavar='FILE', default='hparams.py', help='The file to use for the hyperparameters')
    args = parser.parse_args()

    hp.configure(args.hp_file)  # load hparams from file
    if args.lr is None:
        args.lr = hp.voc_lr
    if args.batch_size is None:
        args.batch_size = hp.voc_batch_size

    paths = Paths(hp.data_path, hp.voc_model_id, hp.tts_model_id)

    batch_size = args.batch_size
    force_train = args.force_train
    train_gta = args.gta
    lr = args.lr

    if not args.force_cpu and torch.cuda.is_available():
        device = torch.device('cuda')
        if batch_size % torch.cuda.device_count() != 0:
            raise ValueError('`batch_size` must be evenly divisible by n_gpus!')
    else:
        device = torch.device('cpu')
    print('Using device:', device)

    print('\nInitialising Model...\n')

    # Instantiate WaveRNN Model
    voc_model = WaveRNN(rnn_dims=hp.voc_rnn_dims,
                        fc_dims=hp.voc_fc_dims,
                        bits=hp.bits,
                        pad=hp.voc_pad,
                        upsample_factors=hp.voc_upsample_factors,
                        feat_dims=hp.num_mels,
                        compute_dims=hp.voc_compute_dims,
                        res_out_dims=hp.voc_res_out_dims,
                        res_blocks=hp.voc_res_blocks,
                        hop_length=hp.hop_length,
                        sample_rate=hp.sample_rate,
                        mode=hp.voc_mode).to(device)

    # Check to make sure the hop length is correctly factorised
    assert np.cumprod(hp.voc_upsample_factors)[-1] == hp.hop_length

    optimizer = optim.Adam(voc_model.parameters())
    restore_checkpoint(paths, voc_model, optimizer, create_if_missing=True)

    train_set, test_set = get_vocoder_datasets(paths.data, batch_size, train_gta)

    total_steps = 10_000_000 if force_train else hp.voc_total_steps

    simple_table([('Remaining', str((total_steps - voc_model.get_step())//1000) + 'k Steps'),
                  ('Batch Size', batch_size),
                  ('LR', lr),
                  ('Sequence Len', hp.voc_seq_len),
                  ('GTA Train', train_gta)])

    loss_func = F.cross_entropy if voc_model.mode == 'RAW' else discretized_mix_logistic_loss

    voc_train_loop(paths, voc_model, loss_func, optimizer, train_set, test_set, lr, total_steps)

    print('Training Complete.')
    print('To continue training increase voc_total_steps in hparams.py or use --force_train')


def voc_train_loop(paths: Paths, model: WaveRNN, loss_func, optimizer, train_set, test_set, lr, total_steps):
    # Use same device as model parameters
    device = next(model.parameters()).device

    for g in optimizer.param_groups: g['lr'] = lr

    total_iters = len(train_set)
    epochs = (total_steps - model.get_step()) // total_iters + 1

    for e in range(1, epochs + 1):

        start = time.time()
        running_loss = 0.

        for i, (x, y, m) in enumerate(train_set, 1):
            x, m, y = x.to(device), m.to(device), y.to(device)

            # Parallelize model onto GPUS using workaround due to python bug
            if device.type == 'cuda' and torch.cuda.device_count() > 1:
                y_hat = data_parallel_workaround(model, x, m)
            else:
                y_hat = model(x, m)

            if model.mode == 'RAW':
                y_hat = y_hat.transpose(1, 2).unsqueeze(-1)

            elif model.mode == 'MOL':
                y = y.float()

            y = y.unsqueeze(-1)


            loss = loss_func(y_hat, y)

            optimizer.zero_grad()
            loss.backward()
            if hp.voc_clip_grad_norm is not None:
                grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), hp.voc_clip_grad_norm)
                if np.isnan(grad_norm):
                    print('grad_norm was NaN!')
            optimizer.step()

            running_loss += loss.item()
            avg_loss = running_loss / i

            speed = i / (time.time() - start)

            step = model.get_step()
            k = step // 1000

            if step % hp.voc_checkpoint_every == 0:
                gen_testset(model, test_set, hp.voc_gen_at_checkpoint, hp.voc_gen_batched,
                            hp.voc_target, hp.voc_overlap, paths.voc_output)
                ckpt_name = f'wave_step{k}K'
                save_checkpoint(paths, model, optimizer,
                                name=ckpt_name, is_silent=True)

            msg = f'| Epoch: {e}/{epochs} ({i}/{total_iters}) | Loss: {avg_loss:.4f} | {speed:.1f} steps/s | Step: {k}k | '
            stream(msg)

        # Must save latest optimizer state to ensure that resuming training
        # doesn't produce artifacts
        save_checkpoint(paths, model, optimizer, is_silent=True)
        model.log(paths.voc_log, msg)
        print(' ')


def save_checkpoint(paths: Paths, model: WaveRNN, optimizer, *,
        name=None, is_silent=False):
    """Saves the training session to disk.

    Args:
        paths:  Provides information about the different paths to use.
        model:  A `WaveRNN` model to save the parameters and buffers from.
        optimizer:  An optmizer to save the state of (momentum, etc).
        name:  If provided, will name to a checkpoint with the given name. Note
            that regardless of whether this is provided or not, this function
            will always update the files specified in `paths` that give the
            location of the latest weights and optimizer state. Saving
            a named checkpoint happens in addition to this update.
    """
    def helper(path_dict, is_named):
        s = 'named' if is_named else 'latest'
        num_exist = sum(p.exists() for p in path_dict.values())

        if num_exist not in (0,2):
            # Checkpoint broken
            raise FileNotFoundError(
                f'We expected either both or no files in the {s} checkpoint to '
                'exist, but instead we got exactly one!')

        if num_exist == 0:
            if not is_silent: print(f'Creating {s} checkpoint...')
            for p in path_dict.values():
                p.parent.mkdir(parents=True, exist_ok=True)
        else:
            if not is_silent: print(f'Saving to existing {s} checkpoint...')

        if not is_silent: print(f'Saving {s} weights: {path_dict["w"]}')
        model.save(path_dict['w'])
        if not is_silent: print(f'Saving {s} optimizer state: {path_dict["o"]}')
        torch.save(optimizer.state_dict(), path_dict['o'])

    latest_paths = {'w': paths.voc_latest_weights, 'o': paths.voc_latest_optim}
    helper(latest_paths, False)

    if name:
        named_paths ={
            'w': paths.voc_checkpoints/f'{name}_weights.pyt',
            'o': paths.voc_checkpoints/f'{name}_optim.pyt',
        }
        helper(named_paths, True)


def restore_checkpoint(paths: Paths, model: WaveRNN, optimizer, *,
        name=None, create_if_missing=False):
    """Restores from a training session saved to disk.

    NOTE: The optimizer's state is placed on the same device as it's model
    parameters. Therefore, be sure you have done `model.to(device)` before
    calling this method.

    Args:
        paths:  Provides information about the different paths to use.
        model:  A `WaveRNN` model to save the parameters and buffers from.
        optimizer:  An optmizer to save the state of (momentum, etc).
        name:  If provided, will restore from a checkpoint with the given name.
            Otherwise, will restore from the latest weights and optimizer state
            as specified in `paths`.
        create_if_missing:  If `True`, will create the checkpoint if it doesn't
            yet exist, as well as update the files specified in `paths` that
            give the location of the current latest weights and optimizer state.
            If `False` and the checkpoint doesn't exist, will raise a
            `FileNotFoundError`.
    """
    if name:
        path_dict = {
            'w': paths.voc_checkpoints/f'{name}_weights.pyt',
            'o': paths.voc_checkpoints/f'{name}_optim.pyt',
        }
        s = 'named'
    else:
        path_dict = {
            'w': paths.voc_latest_weights,
            'o': paths.voc_latest_optim
        }
        s = 'latest'

    num_exist = sum(p.exists() for p in path_dict.values())
    if num_exist == 2:
        # Checkpoint exists
        print(f'Restoring from {s} checkpoint...')
        print(f'Loading {s} weights: {path_dict["w"]}')
        model.load(path_dict['w'])
        print(f'Loading {s} optimizer state: {path_dict["o"]}')
        optimizer.load_state_dict(torch.load(path_dict['o']))
    elif create_if_missing:
        save_checkpoint(paths, model, optimizer, name=name, is_silent=False)
    else:
        raise FileNotFoundError(f'The {s} checkpoint could not be found!')


if __name__ == "__main__":
    main()

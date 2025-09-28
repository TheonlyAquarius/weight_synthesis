import torch
from safetensors.torch import save_file

from latent_diffusion import generate_weights, train_latent_diffusion
from train_ae import FlatWeightsDataset, train_autoencoder


def _create_dummy_dataset(dir_path, num_files: int = 4) -> str:
    dir_path.mkdir(parents=True, exist_ok=True)
    for idx in range(num_files):
        weights = {
            "layer.weight": torch.randn(4, 3),
            "layer.bias": torch.randn(4),
        }
        save_file(weights, str(dir_path / f"sample_{idx}.safetensors"))
    return str(dir_path)


def test_end_to_end_pipeline(tmp_path):
    data_dir = _create_dummy_dataset(tmp_path / "weights")
    ae = train_autoencoder(
        data_dir,
        epochs=1,
        batch_size=2,
        lr=1e-3,
        num_latents=4,
        latent_dim=16,
        depth=1,
    )

    dataset = FlatWeightsDataset(data_dir)
    denoiser, sched = train_latent_diffusion(
        ae,
        dataset,
        epochs=1,
        batch_size=2,
        lr=1e-3,
        T=10,
    )

    flat_weights = generate_weights(ae, denoiser, sched)
    assert flat_weights.shape[0] == dataset.D
    assert torch.isfinite(flat_weights).all()

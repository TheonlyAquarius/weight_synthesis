from train_ae import FlatWeightsDataset, train_autoencoder
from latent_diffusion import train_latent_diffusion, generate_weights


def run_training_pipeline(
    data_dir: str,
    ae_epochs: int = 5,
    ae_batch_size: int = 2,
    ae_lr: float = 1e-4,
    num_latents: int = 64,
    latent_dim: int = 512,
    depth: int = 6,
    diff_epochs: int = 10,
    diff_batch_size: int = 32,
    diff_lr: float = 2e-4,
    diff_T: int = 1000,
):
    ae = train_autoencoder(
        data_dir,
        epochs=ae_epochs,
        batch_size=ae_batch_size,
        lr=ae_lr,
        num_latents=num_latents,
        latent_dim=latent_dim,
        depth=depth,
    )
    dataset = FlatWeightsDataset(data_dir)
    denoiser, sched = train_latent_diffusion(
        ae,
        dataset,
        epochs=diff_epochs,
        batch_size=diff_batch_size,
        lr=diff_lr,
        T=diff_T,
    )
    flat_weights = generate_weights(ae, denoiser, sched)
    return flat_weights, ae, denoiser, sched


if __name__ == "__main__":
    raise SystemExit("This module provides run_training_pipeline() for scripted use.")

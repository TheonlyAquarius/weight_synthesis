import math
from pathlib import Path
from typing import List

import torch
from safetensors.torch import save_file

from diffusion_optimizer import (
    DiffusionSchedule,
    ModelZooDataset,
    WeightDiffusionMLP,
    compute_spectral_properties,
    compute_weight_statistics,
    flatten_state_dict,
    generate_weight_vectors,
    train_synthesizer,
    unflatten_to_state_dict,
)


def _create_toy_state_dict(seed: int = 0) -> dict:
    torch.manual_seed(seed)
    model = torch.nn.Sequential(
        torch.nn.Linear(8, 16),
        torch.nn.ReLU(),
        torch.nn.Linear(16, 4),
    )
    return model.state_dict()


def _write_corpus(tmp_path: Path, count: int) -> List[Path]:
    paths: List[Path] = []
    for idx in range(count):
        state_dict = _create_toy_state_dict(seed=idx)
        path = tmp_path / f"model_{idx}.safetensors"
        save_file(state_dict, str(path))
        paths.append(path)
    return paths


def test_flatten_and_unflatten_roundtrip():
    state_dict = _create_toy_state_dict()
    flat, metadata = flatten_state_dict(state_dict)
    reconstructed = unflatten_to_state_dict(flat, metadata)
    assert set(reconstructed.keys()) == set(state_dict.keys())
    for key, original in state_dict.items():
        restored = reconstructed[key]
        assert restored.shape == original.shape
        assert restored.dtype == original.dtype


def test_dataset_normalization(tmp_path):
    paths = _write_corpus(tmp_path, count=3)
    dataset = ModelZooDataset(paths)
    stacked = torch.stack([dataset[idx] for idx in range(len(dataset))])
    mean = stacked.mean().item()
    std = stacked.std(unbiased=False).item()
    assert abs(mean) < 1e-5
    assert math.isclose(std, 1.0, rel_tol=5e-3)


def test_training_and_generation(tmp_path):
    paths = _write_corpus(tmp_path, count=4)
    dataset = ModelZooDataset(paths)
    schedule = DiffusionSchedule(timesteps=10)
    model = WeightDiffusionMLP(parameter_dim=dataset.feature_dim)

    losses = train_synthesizer(
        model,
        dataset,
        schedule,
        epochs=1,
        batch_size=2,
        lr=1e-3,
    )
    assert losses, "Training did not record any losses"

    mean, std = dataset.stats
    metadata = dataset.metadata_for_index(0)
    generated = generate_weight_vectors(
        model,
        schedule,
        mean,
        std,
        metadata,
        num_samples=1,
    )
    assert len(generated) == 1
    template = unflatten_to_state_dict(dataset[0] * std + mean, metadata)
    for key, tensor in generated[0].items():
        assert tensor.shape == template[key].shape
        assert tensor.dtype == template[key].dtype

    stats = compute_weight_statistics(generated[0])
    assert {"mean", "variance", "std", "kurtosis"}.issubset(stats.keys())

    spectra = compute_spectral_properties(generated[0])
    for values in spectra.values():
        assert values.min().item() >= -1e-5


def test_training_uses_dataset_smaller_than_batch_size(tmp_path):
    paths = _write_corpus(tmp_path, count=2)
    dataset = ModelZooDataset(paths)
    schedule = DiffusionSchedule(timesteps=2)
    model = WeightDiffusionMLP(
        parameter_dim=dataset.feature_dim,
        bottleneck_dim=16,
    )

    losses = train_synthesizer(
        model,
        dataset,
        schedule,
        epochs=2,
        batch_size=len(dataset) + 1,
        lr=1e-3,
    )

    assert len(losses) == 2

import torch
import torch.nn as nn
from safetensors.torch import save_file

from diffusion_optimizer import (
    DiffusionSynthesizer,
    ModelZooCorpus,
    flatten_state_dict,
    unflatten_to_state_dict,
)


def test_flatten_roundtrip_preserves_tensors():
    model = nn.Sequential(nn.Linear(4, 3), nn.LayerNorm(3))
    state_dict = model.state_dict()
    flat, metadata = flatten_state_dict(state_dict)
    reconstructed = unflatten_to_state_dict(flat, metadata)

    for key, tensor in state_dict.items():
        assert torch.allclose(tensor, reconstructed[key])


def _make_toy_state_dict(seed: int) -> dict[str, torch.Tensor]:
    torch.manual_seed(seed)
    model = nn.Sequential(nn.Linear(6, 8), nn.ReLU(), nn.Linear(8, 4))
    return model.state_dict()


def test_model_zoo_corpus_normalization(tmp_path):
    files = []
    for idx in range(3):
        state_dict = _make_toy_state_dict(idx)
        path = tmp_path / f"model_{idx}.safetensors"
        save_file(state_dict, str(path))
        files.append(path)

    corpus = ModelZooCorpus(files)
    normalized = torch.stack([corpus[i][0] for i in range(len(corpus))])

    assert normalized.shape == (len(files), corpus.vector_dim)
    assert torch.allclose(normalized.mean(dim=0), torch.zeros(corpus.vector_dim), atol=1e-5)
    assert torch.allclose(normalized.std(dim=0, unbiased=False), torch.ones(corpus.vector_dim), atol=1e-5)

    recovered = corpus.denormalize(normalized[0])
    assert torch.allclose(recovered, corpus.vectors[0])


def test_diffusion_synthesizer_sampling(tmp_path):
    files = []
    metadata_reference = None
    for idx in range(2):
        state_dict = _make_toy_state_dict(idx + 10)
        path = tmp_path / f"train_{idx}.safetensors"
        save_file(state_dict, str(path))
        files.append(path)
        if metadata_reference is None:
            _, metadata_reference = flatten_state_dict(state_dict)

    corpus = ModelZooCorpus(files)
    synthesizer = DiffusionSynthesizer(corpus.vector_dim, timesteps=16)

    batch = torch.stack([corpus[i][0] for i in range(len(corpus))])
    loss, details = synthesizer.compute_loss(batch)

    assert loss.ndim == 0
    assert torch.isfinite(loss)
    assert details["noisy_weights"].shape == batch.shape

    samples = synthesizer.sample(num_samples=3, device=batch.device)
    assert samples.shape == (3, corpus.vector_dim)

    denormalized = corpus.denormalize(samples[0]).cpu()
    state_dict = unflatten_to_state_dict(denormalized, metadata_reference)
    assert set(state_dict.keys()) == {info.key for info in metadata_reference}

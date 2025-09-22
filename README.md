# Variable-Shape Tensor Generation Notes

## Project Ground Truth
- **Primary objective:** Build a single, architecture-agnostic weight synthesizer that can serve as a universal generator of trained checkpoints.【F:_Variable-Shape Tensor Generation Architectures .txt†L292-L299】
- **Mechanism under study:** The research centers on generative models that operate directly in parameter space rather than data space.【F:_Variable-Shape Tensor Generation Architectures .txt†L295-L303】
- **Empirical insight:** High-quality weights can be synthesized without replaying the original optimization trajectory; the path is independent.【F:_Variable-Shape Tensor Generation Architectures .txt†L299-L302】
- **Training pivot:** Instead of storing entire SGD paths, the plan is to corrupt final checkpoints with Gaussian noise and train a denoiser à la DDPM on this corpus.【F:_Variable-Shape Tensor Generation Architectures .txt†L303-L307】
- **Core obstacle:** Achieving architecture-agnosticism requires handling variable output shapes, which is unrelated to variable-sized inputs at inference time.【F:_Variable-Shape Tensor Generation Architectures .txt†L308-L313】

## Baseline Limitation: Shape-Locked Generators
- A flattened MLP generator couples its input/output dimensions to a specific parameter count, making it incompatible with architectures whose weights differ in total size.【F:_Variable-Shape Tensor Generation Architectures .txt†L105-L116】
- Equating sequence models that accept variable-length inputs with generators that must emit variable-shaped tensors is a category error; they address different problems.【F:_Variable-Shape Tensor Generation Architectures .txt†L128-L134】
- Progress requires dropping the "variable-sized input" analogy and reframing the challenge around conditional weight generation.【F:_Variable-Shape Tensor Generation Architectures .txt†L171-L175】

## Architectures for Variable-Shape Weight Synthesis
- **Per-layer hypernetworks:** Condition a shared generator on each layer specification (e.g., type, channel counts, kernel size) and emit that layer's weights before assembling a full state dict.【F:_Variable-Shape Tensor Generation Architectures .txt†L152-L156】
- **Graph-conditioned generators:** Encode the architecture as a computation graph, let a GNN produce per-node embeddings, and decode weights from those embeddings.【F:_Variable-Shape Tensor Generation Architectures .txt†L157-L160】
- Both approaches sidestep shape-locking by working with structured layer-wise contexts instead of a single flattened tensor.【F:_Variable-Shape Tensor Generation Architectures .txt†L161-L163】

## Action Items Going Forward
1. Focus analysis and design on conditional generators capable of emitting tensors whose shapes follow a supplied specification.【F:_Variable-Shape Tensor Generation Architectures .txt†L171-L183】
2. When evaluating designs, articulate how the conditioning signal is encoded, how outputs match requested shapes, and the trade-offs in inductive bias between hypernetwork and graph-based approaches.【F:_Variable-Shape Tensor Generation Architectures .txt†L179-L183】
3. Maintain alignment with the system state above to avoid the conversational drift that previously derailed progress.【F:_Variable-Shape Tensor Generation Architectures .txt†L185-L186】

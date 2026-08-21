# vLLM with the model weights baked in.
#
# Pulling 5.8 GiB from HuggingFace on every pod start is neither fast nor
# reliable, and a GPU node that spends 90 seconds downloading is a GPU node
# billing at $1/hr to do nothing. The image is ~12 GiB and pulls in ~20s from
# ECR in-region over the 10 Gbps link.
FROM vllm/vllm-openai:v0.7.3

ARG MODEL=Qwen/Qwen3-8B-AWQ

ENV HF_HOME=/models \
    HF_HUB_DISABLE_TELEMETRY=1

RUN python3 -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('${MODEL}', local_dir='/models/${MODEL}', local_dir_use_symlinks=False)"

# curl for the post-start CUDA-graph warm-up probe in the Helm chart.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV VLLM_MODEL_PATH=/models/${MODEL}
EXPOSE 8000

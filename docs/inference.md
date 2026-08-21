# Local inference

## Why an A10G and not something cheaper

Decode on an 8B model is memory-bandwidth-bound, not compute-bound: it reads
the entire weight set once per token. That single fact decides the card.

| Card | VRAM | Bandwidth | On-demand | Verdict |
|---|---|---|---|---|
| L4 (`g6.xlarge`) | 24GB | 300 GB/s | $0.805/hr | 20% cheaper, ~50% slower decode. Bad trade. |
| **A10G (`g5.xlarge`)** | **24GB** | **600 GB/s** | **$1.006/hr** | **Chosen.** |
| L40S (`g6e.xlarge`) | 48GB | 864 GB/s | $1.861/hr | Right answer for 14B+ or FP8. Tier C. |
| A100 40GB | 40GB | 1555 GB/s | $3.06/hr | Overkill for an 8B. |

AWQ over GPTQ because vLLM's AWQ Marlin kernels are faster on Ampere. AWQ over
FP8 because the A10G is `sm_86` and has no native FP8; FP8 becomes correct on
an L40S or H100, which is the Tier C conversation.

## The memory arithmetic

```
Total VRAM                                    24.0 GiB
Usable at gpu_memory_utilization=0.90         21.6 GiB
  Weights (Qwen3-8B AWQ 4-bit)               - 4.3 GiB
  Embedding + lm_head (fp16, 151k vocab)     - 1.5 GiB
  CUDA graphs + activations + workspace      - 1.8 GiB
                                             ---------
  Available for KV cache                      14.0 GiB
```

KV cache per token:

```
2 (K,V) x 36 layers x 8 KV heads x 128 head_dim x 2 bytes (fp16)
= 147,456 bytes = 144 KiB per token
```

Qwen3-8B uses GQA with 8 KV heads against 32 query heads. Without GQA this
would be 576 KiB/token and the whole design would collapse onto a bigger card.

```
14.0 GiB / 144 KiB = ~101,900 tokens of KV cache
```

At `max_model_len=16384` that is 6 concurrent sequences in the worst case; at
the observed ~6k-token mean, about 16. `max_num_seqs=16` matches, because
promising more than the cache can hold just moves the queue inside vLLM where
it is harder to see.

**Why 16k and not the 128k the model supports:** allowing 128k would let one
request consume 18 GiB of KV cache and starve every other. The context
strategy in `cairn_orchestrator/context.py` exists precisely so this number
can stay small.

## Expected performance

These are estimates from the bandwidth arithmetic, not measurements.

| Metric | Value | Basis |
|---|---|---|
| Decode, single stream | ~65 tok/s | 600 GB/s / 5.8 GiB weights x ~65% efficiency |
| Decode, batch 16 | ~780 tok/s aggregate | continuous batching amortizes weight reads |
| Prefill | ~3,000 tok/s | compute-bound, AWQ dequant included |
| TTFT, 4k prompt, warm | ~1.4s | 4,000 / 3,000 + overhead |
| TTFT, prefix cache hit | ~0.5s | only the delta prefills |
| Cold start (KEDA 0 to 1) | ~95s | 55s node + 25s image + 15s weight load |

Reproduce with:

```bash
vllm bench serve \
  --model Qwen/Qwen3-8B-AWQ \
  --dataset-name random --random-input-len 4096 --random-output-len 512 \
  --num-prompts 200 --request-rate 4 --metric-percentiles 50,95,99
```

**Treat the table as falsified if measurements come in more than 20% low.**

## Flags that are not tuning

- `--enable-prefix-caching` — the system prompt plus tool definitions run
  ~2,800 tokens and are byte-identical on every call. This turns that into a
  cache hit and cuts TTFT by roughly 40% at typical load.
- `--enable-chunked-prefill` — stops a 12k prefill from stalling decode for
  everyone else.
- `--disable-log-requests` — a compliance control. Prompts carry restricted
  data and must never be written to a log.

## Cold start

Three mitigations, in order of how much they help:

1. **Weights baked into the image** (`docker/vllm.Dockerfile`). A 12 GiB image
   pulls in ~20s from ECR in-region; pulling 5.8 GiB from HuggingFace on every
   pod start is neither fast nor reliable.
2. **hostPath cache on the GPU node.** Node reuse across pod restarts skips the
   pull entirely.
3. **Warm pool at Tier C.** `minReplicas: 1`. Below that volume the ~$180/month
   of idle GPU does not justify the 95 seconds it saves.

The post-start hook fires one synthetic completion to trigger CUDA graph
capture before the pod accepts traffic. Without it, the first real request
eats ~8 seconds of graph capture and looks like a latency spike.

## Scaling

KEDA scales on queue depth and KV-cache usage, never GPU utilisation. GPU
utilisation sits near 100% during any decode and tells you nothing about
whether the tier is saturated. KV cache above 75% means vLLM is about to start
preempting sequences — that is the honest signal, and there is an alert on
`vllm:num_preemptions_total` for when it does.

The 600-second cooldown is deliberate and is matched by Karpenter's
`consolidateAfter: 10m`. Two autoscalers with different opinions about when a
node is idle is a thrash loop that bills by the hour.

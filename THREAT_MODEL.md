# Threat model

**Status: DRAFT** — prepared for the prototype; to be reviewed with the adopting team's security
reviewers before any production use.

## Scope and assets

Simulated edge boxes publish over MQTT to AWS IoT Core, upload images to S3 through presigned POSTs,
and a Step Functions workflow sends a sanitized copy of each image to an Amazon Bedrock model that
drafts e-commerce copy and a subject box. Results land on the "shelf" (S3 objects plus a manifest).

Assets: device identities (certificates and private keys), the claim certificate, uploaded images,
shelf content (published copy and images), per-device data isolation, the model and its budget, the
deployment channel to edge devices (Greengrass components, IoT jobs).

## Trust boundaries

```mermaid
flowchart LR
  subgraph Untrusted
    D[Edge box / simulator] -->|MQTT 5 mTLS| IOT
    D -->|presigned POST| S3U[(S3 uploads/)]
    IMG[Image content, text in image, metadata]
    OUT[Model output]
  end
  subgraph Account [AWS account - trusted control plane]
    IOT[IoT Core: device policy + rules] --> Q[SQS ingest] --> SF[Step Functions]
    SF --> V[validate_input] --> A[analyze] --> F[finalize / fallback]
    A -->|Converse + ApplyGuardrail| BR[Amazon Bedrock]
    F --> SHELF[(S3 shelf/ + manifest)]
  end
  S3U --> V
  BR --> OUT --> A
```

Device payloads, image bytes and everything the model returns cross into the account as untrusted
data. Operators reach the account only through IAM.

## Threats and controls

Each control names the file that implements it. Tests are under `tests/`; the cloud evidence is in
VERIFICATION.md.

### T1 Prompt injection through text in the image

Instructions drawn into the scene ("ignore the previous instructions", multilingual, faint text, text
walls), hidden characters, or text in image metadata.

| Control | Where |
|---|---|
| Metadata never reaches the model: every upload is decoded and re-encoded as a fresh JPEG without EXIF/text chunks | `src/edge_pipeline/image_checks.py` |
| System prompt states that text in the image is scene content, never instructions; output only through one forced tool (`toolChoice.tool`) | `src/edge_pipeline/prompts.py`, `src/edge_pipeline/model.py` |
| Output schema enforced in code (enums, lengths, `additionalProperties: false`, box ranges, NaN/Infinity) | `src/edge_pipeline/validation.py` |
| Content rules on copy: URLs, e-mail, phone numbers, markup, instruction-like phrases (EN/ZH), device/certificate identifiers, price/promo claims, control and bidi characters | `src/edge_pipeline/validation.py` |
| Model-reported instruction text or poor quality → neutral copy, `needs_review` | `src/edge_pipeline/validation.py`, `src/edge_pipeline/pipeline.py` |
| Bedrock Guardrails: image content filters including PROMPT_ATTACK on the input image; content filters on the draft copy | `infra/edge_infra/functions.py`, `src/edge_pipeline/guardrail.py` |
| Synthetic test set with a model stand-in that obeys the injected text, and a negative control that must fail | `src/edge_pipeline/synthetic.py`, `src/edge_pipeline/evaluation.py`, `tests/test_scenarios_local.py` |

Known gaps (documented, tested as "known_gap"): persuasive wording without URLs or keywords passes
the code checks (case `i12-subtle-claim`); the keyword lists are not exhaustive; Guardrails evaluate
only part of the text in an image (see the Guardrails documentation). Mitigation in production:
human review for new sellers/devices, stricter allow-lists for copy.

### T2 Device identity theft and impersonation

| Control | Where |
|---|---|
| One X.509 certificate per device; private keys generated in the device (simulator memory) through fleet provisioning, or written straight into Secrets Manager for Greengrass cores | `src/edge_pipeline/simulator/provisioning.py`, `scripts/setup_identities.py` |
| `iot:Connect` only as the client id equal to the certificate's thing, and only if the certificate is attached (`IsAttached`) | `src/edge_pipeline/policy.py` |
| Claim certificate limited to the provisioning topics; pre-provisioning hook checks serial format, fleet size and that the claim client id matches the serial | `src/edge_pipeline/policy.py`, `src/functions/handlers.py` |
| Revocation: `UpdateCertificate` to INACTIVE/REVOKED closes and refuses connections; cleanup detaches, deactivates and deletes | `scripts/e2e_aws.py` (probe), `scripts/cleanup_iot.py` |
| Greengrass components cannot read the ECS task role: the entrypoint removes the container-credential variables before the nucleus starts | `containers/greengrass/entrypoint.sh` |

Residual: the shared claim certificate, if extracted, lets an attacker register serials allowed by
the hook. Production: per-device unique certificates at manufacturing (JITP/JITR) or provisioning by
trusted user, hook checks against an allow-list, and claim rotation.

### T3 A device accessing another device's data

| Control | Where |
|---|---|
| Publish/subscribe/receive only on `edge/<own thing>/...` and the device's own job topics (thing policy variable) | `src/edge_pipeline/policy.py` |
| Rules take the identity from the topic and require it to equal `clientid()` | `src/edge_pipeline/rules.py` |
| Presigned POST pins one key under the device's own prefix, the content type and the exact size | `src/edge_pipeline/store.py` |
| The dispatcher claims `(thing, image_id)` conditionally: foreign, unknown and replayed ids are dropped | `src/edge_pipeline/store.py`, `src/edge_pipeline/pipeline.py` |
| Results are published to the owning device's topic only; replies contain status and codes | `src/edge_pipeline/pipeline.py` |
| Bucket resource policy: object reads/writes only by the stack's roles and named operators | `infra/edge_infra/stack.py` |

### T4 Model output with improper content or other devices' data

The model sees one sanitized image per call and no other device's data (no retrieval, no history,
no tools), so it has nothing of other devices to leak; identifiers in copy are still rejected. Toxic
content: Guardrails content filters on the copy; offensive text without keywords may pass (gap).
Logs carry ids, enums, codes and timings only (`src/edge_pipeline/metrics.py`); validation issues
carry field paths and keywords, never values.

### T5 Uploaded file type spoofing and parser vulnerabilities

| Control | Where |
|---|---|
| Declared type, size and SHA-256 checked before a URL is issued and again on the bytes | `src/edge_pipeline/image_checks.py` |
| Magic number must match the declared type; Pillow format must match; only JPEG/PNG; single frame | `src/edge_pipeline/image_checks.py` |
| Pixel cap with decompression-bomb detection; dimension limits; decode errors become a reason code | `src/edge_pipeline/image_checks.py` |
| Parsing happens in a short-lived Lambda with read access to `uploads/` and write access to `work/` only | `infra/edge_infra/functions.py` |
| Pillow updates are grouped separately in Dependabot; osv-scanner runs on the lock file | `.github/dependabot.yml`, `Makefile` |

Residual: Pillow and its codecs parse untrusted input; a codec vulnerability would run inside the
validate function's sandbox. Production: keep Pillow patched, consider a separate account for parsing.

### T6 Abuse: invocation spikes and cost

| Control | Where |
|---|---|
| Per-device hourly upload limit (DynamoDB conditional counter) before any URL is issued | `src/edge_pipeline/pipeline.py`, `src/edge_pipeline/store.py` |
| Fleet size limited by the pre-provisioning hook | `src/functions/handlers.py` |
| SQS buffer with event-source maximum concurrency in front of the workflow | `infra/edge_infra/functions.py` |
| Alarms: Bedrock invocation volume, fallback spikes, DLQ, rule errors, failed workflows; optional cost budget | `infra/edge_infra/observe.py` |
| IoT Core per-connection and per-account limits (service quotas) | AWS IoT Core |

### T7 Malicious or broken edge release

Staged rollouts with abort criteria and per-device rollback (Greengrass `failureHandlingPolicy:
ROLLBACK`, IoT Jobs abort), health checks in the component (`greengrass/com.example.EdgeModel/selftest.py`,
`src/edge_pipeline/simulator/device.py`), artifacts only from this project's S3 prefix through the
token-exchange role (`infra/edge_infra/iot.py`). Residual: no artifact signing in the prototype
(hardening list).

### T8 Operator and pipeline misconfiguration

Parameter validation at synth (`infra/edge_infra/params.py`), cdk-nag with acknowledgements per
construct (`infra/edge_infra/nag.py`), least-privilege roles per function, trust policies with
`aws:SourceAccount`/`aws:SourceArn` where documented (`infra/edge_infra/iot.py`, `infra/edge_infra/stack.py`),
destructive-change guard (`scripts/deploy.sh`), secret scanning with a canary (CI).

## Out of scope

Physical attacks on devices, the device operating system, a real e-commerce platform, multi-tenant
separation between sellers, and the security of the operator workstation.

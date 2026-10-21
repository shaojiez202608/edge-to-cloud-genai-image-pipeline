# Open questions

Technical and business questions the prototype could not answer. Each lists the assumption used.

| # | Question | Assumption in the prototype |
|---|---|---|
| Q1 | Fleet size, connection pattern (always-on vs. periodic), heartbeat interval and image rate per device | Load test sized from the account's quotas and `max_fleet_size`; heartbeat and upload rates are parameters |
| Q2 | Is deployment in China regions required? Bedrock is not in the China service list and Greengrass V2 is listed for Beijing only | Default region `us-east-1`; a China deployment needs another model hosting option (docs/architecture.md, region differences) |
| Q3 | Image types (resolution, formats, multiple products per frame, people in frame) | JPEG/PNG, one product per image, synthetic flat renderings; no people |
| Q4 | What "processing" means for the shelf: background removal, retouching, generated backgrounds, copy languages, tone, mandatory attributes | Deterministic crop on a plain canvas + draft title/bullets/tags in one locale (`copy_locale`) |
| Q5 | Who reviews `needs_review` items and what the publication rule is (auto-publish vs. always reviewed) | Model copy is published only when every check passes; otherwise neutral copy and review |
| Q6 | How devices get identities at manufacturing (own CA → JITP/JITR, installer app → trusted user, none → claim) | Fleet provisioning by claim for simulators; pre-created certificates for Greengrass cores |
| Q7 | Is Greengrass already on the edge boxes, and which OS/architecture do they run? | Greengrass nucleus in a container on ARM64; lightweight boxes use the IoT Device SDK |
| Q8 | Release policy for edge components: canary size, abort thresholds, who approves | Exponential rollout starting with a small rate, abort at a failure percentage after a minimum number of devices (parameters in the drill scripts) |
| Q9 | Data retention for uploads, results and shelf, and whether images may contain personal data | Short lifecycle expirations per prefix (parameter file); synthetic data only |
| Q10 | Budget owner and alert recipients for model spend; acceptable model latency | Optional account-wide budget, alarm topic without subscribers unless `alarm_email` is set |
| Q11 | Which other e-commerce artefacts are needed (attribute schemas, category taxonomies) | A fixed category enum in the pipeline config |
| Q12 | GitHub maintainer handle and publication location | CODEOWNERS uses `@shaojiez202608`; nothing is pushed |

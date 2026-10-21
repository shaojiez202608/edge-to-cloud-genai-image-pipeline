# Prior art

Search date: 2026-10-21. Sources: aws-samples, awslabs and aws-solutions on GitHub, AWS
documentation, AWS Prescriptive Guidance, AWS blogs. Each link below was opened on that date.

The question was whether a public AWS asset already combines: (1) per-device identity and isolation on
AWS IoT Core, (2) a serverless generative-AI image pipeline that turns uploads into e-commerce
assets, (3) a connection load-test and reconnect-storm method, (4) a staged rollout with automatic
rollback drill for edge components, and (5) prompt-injection defences with a synthetic test set for
text embedded in images.

| Asset | Covers | Does not cover |
|---|---|---|
| [aws-samples/aws-smart-product-onboarding](https://github.com/aws-samples/aws-smart-product-onboarding) | Product images and metadata through Step Functions and Lambda to Amazon Bedrock; title, description, category, attributes | Device ingest, image cropping/shelf output, prompt-injection tests |
| [aws-samples/sample-ai-powered-product-catalog](https://github.com/aws-samples/sample-ai-powered-product-catalog) | Web upload to S3, Step Functions, Nova models for attributes and descriptions, status in DynamoDB | Device ingest, prompt-injection tests |
| [aws-samples/sample-camera-edge-data-intelligence-transformation-with-bedrock](https://github.com/aws-samples/sample-camera-edge-data-intelligence-transformation-with-bedrock) | Camera streams or batch uploads, frame pre-filtering, Bedrock image analysis | Per-device X.509 identity and isolation, e-commerce output, load tests, staged rollout, injection defences |
| [awslabs/aws-greengrass-labs-s3-file-uploader](https://github.com/awslabs/aws-greengrass-labs-s3-file-uploader) | Greengrass component that uploads files to S3 with the token exchange role | Cloud pipeline, GenAI, rollout drills |
| [AWS IoT thing policy variables](https://docs.aws.amazon.com/iot/latest/developerguide/thing-policy-variables.html) | `iot:Connection.Thing.ThingName`, `IsAttached` for one policy across many devices | A runnable pipeline that proves cross-device denial end to end |
| [aws-samples/serverless-load-test-IoT-Core](https://github.com/aws-samples/serverless-load-test-IoT-Core) | MQTT/HTTP/WebSocket load generation from Lambda or EC2, connect and pub/sub latency | Reconnect storms, per-device certificates |
| [aws-samples/iot-network-traffic-control-and-load-testing-simulator](https://github.com/aws-samples/iot-network-traffic-control-and-load-testing-simulator) | Device simulation on Fargate with network fault injection | Reconnect-storm method, certificate strategy (last updated 2023) |
| [aws-samples/mqtt-load-test-generator](https://github.com/aws-samples/mqtt-load-test-generator) (archived) | One X.509 client, numbered messages, loss and latency through Firehose/Athena | Many connections, reconnect storms |
| [aws-solutions/iot-device-simulator](https://github.com/aws-solutions/iot-device-simulator) (deprecated, archived) | GUI to simulate many devices | Reconnect storms, per-device certificates; no longer maintained |
| [IoT Lens – failure management](https://docs.aws.amazon.com/wellarchitected/latest/iot-lens/failure-management.html) | Guidance: backoff with jitter, lifecycle events, persistent sessions | A runnable harness or measurements |
| [IoT Jobs rollout/abort configuration](https://docs.aws.amazon.com/iot/latest/developerguide/jobs-configurations-details.html), [Greengrass deployments](https://docs.aws.amazon.com/greengrass/v2/developerguide/create-deployments.html) | Rollout rates, abort criteria, timeouts, Greengrass `failureHandlingPolicy` | A scripted drill with a deliberately broken release and evidence of who received it |
| [aws-samples/ros2-ota-firmware-updates](https://github.com/aws-samples/ros2-ota-firmware-updates) | IoT Jobs and Greengrass OTA for a fleet | Staged rollout with abort, rollback evidence |
| [Bedrock Guardrails image content filters](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-mmfilter.html) | Image filtering through ApplyGuardrail, including the prompt-attack category for images in GA regions | A test set; code-level output validation |
| [Security blog: prompt injection](https://aws.amazon.com/blogs/security/safeguard-your-generative-ai-workloads-from-prompt-injections/), [Prescriptive Guidance: common attacks](https://docs.aws.amazon.com/prescriptive-guidance/latest/llm-prompt-engineering-best-practices/common-attacks.html) | Layered defences and an attack taxonomy for text prompts | Images or multimodal vectors, a synthetic image test set |

## Conclusion

No public asset found combines the five parts. Each part exists on its own: device isolation is
documented, image-to-product-copy pipelines exist (without device ingest), load-test tools exist
(several archived, none with a reconnect-storm method), rollout and rollback are service settings
without a public drill, and text-in-image prompt injection has guidance and a Guardrails filter but no
public test set.

## Differences of this prototype

1. **One chain from device to shelf**: simulated edge boxes with their own certificates, policy
   variables and fleet provisioning by claim, MQTT routing through IoT rules, presigned uploads,
   Step Functions and Bedrock, and a shelf manifest in S3. The cross-device denial is tested in the
   cloud, not only described.
2. **Connection load test with a reconnect storm**: a repeatable method (ramp, steady state,
   simultaneous outage, reconnect with or without jitter) that reports connect success, reconnect
   time distribution and end-to-end message loss, sized from the account's Service Quotas.
3. **Rollout and rollback drills**: a deliberately broken component release on Greengrass cores and
   on IoT Jobs simulators with rollout rates and abort criteria, with evidence of which devices got
   it, which rolled back and which never received it.
4. **Multimodal prompt-injection defences with a synthetic test set**: images with embedded
   instructions (visible, faint, multilingual, metadata-only), a "naive model" stand-in that obeys
   them, code-level output validation plus an optional guardrail, a negative control that must fail,
   and documented known gaps.

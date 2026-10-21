# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-21

### Added
- Simulated edge boxes (AWS IoT Device SDK v2, MQTT 5) with fleet provisioning by claim, heartbeats with
  store-and-forward, presigned image uploads, authorization probes and an AWS IoT Jobs agent.
- Per-device AWS IoT policy with the thing policy variable; IoT rules that bind identity to the client id.
- Serverless image pipeline: upload grants, SQS buffer, Step Functions Standard workflow, Amazon Bedrock
  (Nova 2 Lite, forced tool) with Bedrock Guardrails, output validation, shelf images and manifest.
- Prompt-injection synthetic test set, evaluation with a negative control.
- Connection load test with a reconnect storm sized from Service Quotas.
- Rollout and rollback drills on IoT Jobs simulators and on Greengrass V2 cores (ARM64 Fargate).
- Local simulation mode (in-process MQTT broker with policy enforcement and service stand-ins).
- CDK stack with cdk-nag, parameter templates, preflight, guarded deploy, cleanup and residual check.

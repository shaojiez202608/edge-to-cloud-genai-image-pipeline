"""Local simulation mode: in-process stand-ins for AWS IoT Core (MQTT broker with policy checks,
rules engine, lifecycle events, Jobs), S3, DynamoDB, SQS and Step Functions. No broker process, no
containers, no network. These stand-ins model the documented behaviour this prototype relies on;
they are not a reimplementation of the services and do not prove cloud behaviour.
"""

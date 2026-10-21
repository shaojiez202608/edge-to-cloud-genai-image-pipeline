"""Observability and abuse limits: alarm topic, alarms (dead letters, rule errors, failed workflows,
fallback spikes, Bedrock invocation volume), a dashboard with AWS IoT and pipeline metrics, and an
optional AWS Budgets cost budget that notifies the alarm topic."""

from __future__ import annotations

from aws_cdk import Aws, Duration
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cwa
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs


def build(stack) -> None:
    d, prefix, cfg = stack.deploy, stack.prefix, stack.pipeline_cfg
    ns = f"{prefix}/pipeline"
    # No SSE on purpose: CloudWatch alarms and AWS Budgets cannot publish to a topic encrypted with the AWS
    # managed key, and messages carry only alarm names and states (SECURITY-NOTES, SNS2).
    stack.alarm_topic = sns.Topic(stack, "Alarms", topic_name=f"{prefix}-alarms", enforce_ssl=True)
    if d.get("alarm_email"):
        stack.alarm_topic.add_subscription(subs.EmailSubscription(d["alarm_email"]))
    action = cwa.SnsAction(stack.alarm_topic)

    def alarm(cid: str, metric: cw.IMetric, threshold: float, desc: str) -> cw.Alarm:
        a = cw.Alarm(stack, f"Alarm{cid}", alarm_name=f"{prefix}-{cid.lower()}", metric=metric, threshold=threshold,
                     evaluation_periods=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                     treat_missing_data=cw.TreatMissingData.NOT_BREACHING, alarm_description=desc)
        a.add_alarm_action(action)
        return a

    five = Duration.minutes(5)
    alarm("IngestDlq", stack.ingest_dlq.metric_approximate_number_of_messages_visible(period=five), 1,
          "Upload metadata messages could not be dispatched after retries")
    alarm("RuleErrors", stack.rule_errors.metric_approximate_number_of_messages_visible(period=five), 1,
          "An IoT rule action failed (error action queue)")
    alarm("WorkflowFailed", stack.state_machine.metric_failed(period=five), 1,
          "A workflow execution failed even after the fallback step")
    alarm("Fallbacks", cw.Metric(namespace=ns, metric_name="Processed", dimensions_map={"Status": "fallback"},
                                 statistic="Sum", period=Duration.minutes(15)), 5,
          "Many images ended on the fallback path (model unavailable, input rejected or output rejected)")
    bedrock_calls = cw.Metric(namespace="AWS/Bedrock", metric_name="Invocations",
                              dimensions_map={"ModelId": cfg.model_id}, statistic="Sum", period=five)
    alarm("BedrockVolume", bedrock_calls, d["bedrock_invocations_alarm_per_5min"],
          "Model invocation volume above the expected level (possible abuse or runaway devices)")

    iot_metric = lambda name: cw.Metric(namespace="AWS/IoT", metric_name=name, statistic="Sum", period=five)  # noqa: E731
    pm = lambda name, stat="Sum", **dims: cw.Metric(namespace=ns, metric_name=name, statistic=stat,  # noqa: E731
                                                    dimensions_map=dims or None, period=five)
    stack.dashboard = cw.Dashboard(stack, "Dashboard", dashboard_name=f"{prefix}-pipeline")
    stack.dashboard.add_widgets(
        cw.GraphWidget(title="IoT connections (account-wide)", width=12,
                       left=[iot_metric("Connect.Success"), iot_metric("Connect.AuthError"),
                             iot_metric("Connect.Throttle"), iot_metric("Connect.ClientIDThrottle")]),
        cw.GraphWidget(title="IoT messages and denials (account-wide)", width=12,
                       left=[iot_metric("PublishIn.Success"), iot_metric("PublishIn.AuthError"),
                             iot_metric("Subscribe.AuthError"), iot_metric("PublishOut.Success")]),
        cw.GraphWidget(title="Pipeline outcomes", width=12,
                       left=[pm("Processed", Status=s) for s in ("listed", "needs_review", "fallback")]
                       + [pm("UploadRefused", Reason="rate_limited")]),
        cw.GraphWidget(title="Latency (ms)", width=12,
                       left=[pm("EndToEndLatency", "p50"), pm("EndToEndLatency", "p95"), pm("ModelLatency", "p95")]),
        cw.GraphWidget(title="Queues and workflow", width=12,
                       left=[stack.ingest.metric_approximate_age_of_oldest_message(period=five),
                             stack.state_machine.metric_failed(period=five), bedrock_calls]),
    )

    b = d["budget"]
    if b["enabled"]:
        stack.alarm_topic.add_to_resource_policy(iam.PolicyStatement(
            actions=["sns:Publish"], principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
            resources=[stack.alarm_topic.topic_arn],
            conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                        "ArnLike": {"aws:SourceArn": f"arn:{Aws.PARTITION}:budgets::{Aws.ACCOUNT_ID}:*"}}))
        budgets.CfnBudget(stack, "Budget", budget=budgets.CfnBudget.BudgetDataProperty(
            budget_name=f"{prefix}-monthly", budget_type="COST", time_unit="MONTHLY",
            budget_limit=budgets.CfnBudget.SpendProperty(amount=b["monthly_limit_usd"], unit="USD")),
            notifications_with_subscribers=[budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    comparison_operator="GREATER_THAN", notification_type=t, threshold=80, threshold_type="PERCENTAGE"),
                subscribers=[budgets.CfnBudget.SubscriberProperty(subscription_type="SNS",
                                                                  address=stack.alarm_topic.topic_arn)])
                for t in ("ACTUAL", "FORECASTED")])

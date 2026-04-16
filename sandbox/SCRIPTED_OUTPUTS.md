# Scripted outputs — AWS sandbox

When Opus proposes an `aws ...` command during your sandbox investigation,
find the **closest match** below and paste the corresponding output block
back into the Ghosthunter prompt.

The scenario baked into `aws-sandbox-billing.csv` is:

> A Lambda function deployed in mid-February (~2026-02-19) started pulling
> large payloads from S3 in another region. The function logs at DEBUG
> level and runs every minute. Three correlated spikes in the billing:
>
> - **Lambda**: +1515% — the function itself (GB-seconds)
> - **EC2-Other**: +640% — NAT Gateway processing the S3 egress
> - **CloudWatch**: +182% — log ingestion from the DEBUG logs
>
> Root cause Opus should land on: a cross-region S3 read through NAT
> (should have been same-region + S3 VPC endpoint), amplified by DEBUG
> logging. The fix is: same-region S3, S3 gateway VPC endpoint, INFO
> level logs.

---

## Step 0 — Opus will almost certainly ask for this first

**Likely command:** `aws sts get-caller-identity` or similar

```json
{
    "UserId": "AIDA2SANDBOXEXAMPLE:avinashs",
    "Account": "111122223333",
    "Arn": "arn:aws:iam::111122223333:user/avinashs"
}
```

---

## Cost Explorer drill-down: EC2 - Other by USAGE_TYPE

**Likely command:** `aws ce get-cost-and-usage ... --filter
'{"Dimensions":{"Key":"SERVICE","Values":["EC2 - Other"]}}' --group-by
Type=DIMENSION,Key=USAGE_TYPE --output json | jq '...'`

Opus tends to ask this as the very first drill-down when EC2 - Other is
the top spike. The jq pipeline in its command projects the raw CE
response into a sorted `[{type, total}]` array, so the paste-back is the
projected output:

```json
[
  {"type": "NatGateway-Bytes", "total": 10850.00},
  {"type": "PublicIPv4:InUseAddress", "total": 2340.00},
  {"type": "NatGateway-Hours", "total": 1680.00},
  {"type": "EBS:VolumeUsage.gp3", "total": 1125.00},
  {"type": "DataTransfer-Regional-Bytes", "total": 620.00},
  {"type": "EBS:SnapshotUsage", "total": 210.00},
  {"type": "VpcEndpoint-Hours", "total": 125.00},
  {"type": "EBS:VolumeIOUsage", "total": 85.00},
  {"type": "EBS:VolumeP-IOPS.io2", "total": 20.00}
]
```

That tells Opus: **NAT Gateway bytes + hours dominate the spike**
(~$12.5k of the $14.7k delta). It should eliminate the EBS hypotheses
and pivot to NAT investigation, proposing `aws ec2 describe-nat-gateways`
next.

---

## Cost Explorer drill-down: Amazon CloudWatch or AWS Lambda by USAGE_TYPE

If Opus runs a similar CE query against CloudWatch or Lambda, here are
the paste-backs that fit the baked-in scenario:

**For `CloudWatch`:**
```json
[
  {"type": "DataProcessing-Bytes", "total": 1120.00},
  {"type": "TimedStorage-ByteHrs", "total": 380.00},
  {"type": "CW:Requests", "total": 95.00},
  {"type": "CW:MetricMonitorUsage", "total": 45.00},
  {"type": "CW:AlarmMonitorUsage", "total": 3.00}
]
```
(tells Opus: CloudWatch Logs data ingestion dominates — consistent with
DEBUG logging from the payload-enricher Lambda)

**For `AWS Lambda`:**
```json
[
  {"type": "Lambda-GB-Second", "total": 4180.00},
  {"type": "Request", "total": 85.00},
  {"type": "Lambda-Provisioned-Concurrency", "total": 42.00},
  {"type": "Lambda-Edge-GB-Second", "total": 5.00}
]
```
(tells Opus: GB-seconds — i.e. memory × duration × invocations — drives
the Lambda cost. Steers toward `lambda list-functions` + `get-function`
to find the 3008 MB / 48s / 1440-inv/day payload-enricher.)

---

## Lambda investigation

### `aws lambda list-functions --output json`

```json
{
  "Functions": [
    {
      "FunctionName": "user-service-api",
      "Runtime": "nodejs20.x",
      "MemorySize": 512,
      "Timeout": 30,
      "LastModified": "2025-11-14T08:22:01.000+0000",
      "CodeSize": 2418384
    },
    {
      "FunctionName": "event-processor",
      "Runtime": "python3.12",
      "MemorySize": 256,
      "Timeout": 15,
      "LastModified": "2025-09-03T14:17:09.000+0000",
      "CodeSize": 891234
    },
    {
      "FunctionName": "payload-enricher",
      "Runtime": "python3.12",
      "MemorySize": 3008,
      "Timeout": 300,
      "LastModified": "2026-02-19T10:14:33.000+0000",
      "CodeSize": 15728640
    },
    {
      "FunctionName": "billing-webhook-handler",
      "Runtime": "python3.12",
      "MemorySize": 256,
      "Timeout": 10,
      "LastModified": "2024-08-11T22:30:15.000+0000",
      "CodeSize": 412334
    }
  ]
}
```

Note: `payload-enricher` was **last modified 2026-02-19** — exactly when
the spike started. 3008 MB memory (max tier), 5 min timeout. Obvious
suspect.

---

### `aws lambda get-function --function-name payload-enricher --output json`

```json
{
  "Configuration": {
    "FunctionName": "payload-enricher",
    "Runtime": "python3.12",
    "MemorySize": 3008,
    "Timeout": 300,
    "LastModified": "2026-02-19T10:14:33.000+0000",
    "Role": "arn:aws:iam::111122223333:role/payload-enricher-role",
    "Environment": {
      "Variables": {
        "LOG_LEVEL": "DEBUG",
        "S3_BUCKET": "customer-payloads-us-west-2",
        "BATCH_SIZE": "200"
      }
    },
    "Handler": "handler.process_batch",
    "Description": "Enriches payloads with downstream service data"
  },
  "Code": {
    "RepositoryType": "S3",
    "Location": "https://awslambda-us-east-1.s3.us-east-1.amazonaws.com/..."
  }
}
```

Key details:
- `LOG_LEVEL=DEBUG` — explains CloudWatch spike
- `S3_BUCKET=customer-payloads-us-west-2` — **cross-region** read (Lambda
  is in us-east-1, bucket is in us-west-2). Explains NAT / EC2-Other spike.

---

### `aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations --dimensions Name=FunctionName,Value=payload-enricher --start-time 2026-02-01T00:00:00Z --end-time 2026-03-26T00:00:00Z --period 86400 --statistics Sum`

```json
{
  "Label": "Invocations",
  "Datapoints": [
    {"Timestamp": "2026-02-19T00:00:00Z", "Sum": 720.0, "Unit": "Count"},
    {"Timestamp": "2026-02-20T00:00:00Z", "Sum": 1440.0, "Unit": "Count"},
    {"Timestamp": "2026-02-21T00:00:00Z", "Sum": 1440.0, "Unit": "Count"},
    {"Timestamp": "2026-03-01T00:00:00Z", "Sum": 1440.0, "Unit": "Count"},
    {"Timestamp": "2026-03-15T00:00:00Z", "Sum": 1440.0, "Unit": "Count"},
    {"Timestamp": "2026-03-25T00:00:00Z", "Sum": 1440.0, "Unit": "Count"}
  ]
}
```

1440 invocations/day = every minute. Started 2026-02-19.

---

### `aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Duration --dimensions Name=FunctionName,Value=payload-enricher --start-time 2026-03-01T00:00:00Z --end-time 2026-03-26T00:00:00Z --period 86400 --statistics Average,Maximum`

```json
{
  "Label": "Duration",
  "Datapoints": [
    {"Timestamp": "2026-03-15T00:00:00Z", "Average": 47200.0, "Maximum": 62100.0, "Unit": "Milliseconds"},
    {"Timestamp": "2026-03-16T00:00:00Z", "Average": 48400.0, "Maximum": 61800.0, "Unit": "Milliseconds"},
    {"Timestamp": "2026-03-20T00:00:00Z", "Average": 49100.0, "Maximum": 63400.0, "Unit": "Milliseconds"}
  ]
}
```

**~48 seconds average** at 3008 MB memory. That's the Lambda cost driver.

---

## EC2 / NAT investigation (EC2-Other column)

### `aws ec2 describe-nat-gateways --region us-east-1 --output json`

```json
{
  "NatGateways": [
    {
      "NatGatewayId": "nat-0a1b2c3d4e5f6a7b8",
      "VpcId": "vpc-0fedcba987654321",
      "SubnetId": "subnet-0123456789abcdef0",
      "State": "available",
      "CreateTime": "2025-06-14T09:12:00.000Z",
      "NatGatewayAddresses": [{"PublicIp": "52.1.2.3", "PrivateIp": "10.0.1.50"}]
    }
  ]
}
```

Single NAT gateway in us-east-1.

---

### `aws cloudwatch get-metric-statistics --namespace AWS/NATGateway --metric-name BytesOutToDestination --dimensions Name=NatGatewayId,Value=nat-0a1b2c3d4e5f6a7b8 --start-time 2026-02-01T00:00:00Z --end-time 2026-03-26T00:00:00Z --period 86400 --statistics Sum`

```json
{
  "Label": "BytesOutToDestination",
  "Datapoints": [
    {"Timestamp": "2026-02-10T00:00:00Z", "Sum": 195000000000.0, "Unit": "Bytes"},
    {"Timestamp": "2026-02-15T00:00:00Z", "Sum": 198000000000.0, "Unit": "Bytes"},
    {"Timestamp": "2026-02-19T00:00:00Z", "Sum": 680000000000.0, "Unit": "Bytes"},
    {"Timestamp": "2026-02-20T00:00:00Z", "Sum": 2340000000000.0, "Unit": "Bytes"},
    {"Timestamp": "2026-03-01T00:00:00Z", "Sum": 2380000000000.0, "Unit": "Bytes"},
    {"Timestamp": "2026-03-20T00:00:00Z", "Sum": 2420000000000.0, "Unit": "Bytes"}
  ]
}
```

Step-change on 2026-02-19 from **~195 GB/day to ~2.4 TB/day**. Same date
as the Lambda deploy. 12x increase.

---

### `aws ec2 describe-vpc-endpoints --region us-east-1 --output json`

```json
{
  "VpcEndpoints": []
}
```

**Zero VPC endpoints.** So all S3 traffic from Lambda → NAT → public S3.

---

### `aws s3api get-bucket-location --bucket customer-payloads-us-west-2`

```json
{
  "LocationConstraint": "us-west-2"
}
```

Confirms the **cross-region** read. Lambda in us-east-1, bucket in us-west-2.

---

## CloudWatch Logs investigation

### `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/payload-enricher --output json`

```json
{
  "logGroups": [
    {
      "logGroupName": "/aws/lambda/payload-enricher",
      "creationTime": 1740000000000,
      "retentionInDays": null,
      "storedBytes": 4832000000000
    }
  ]
}
```

**4.8 TB stored**, no retention policy. At $0.50/GB-month ingestion + storage, matches the CloudWatch spike.

---

### `aws logs filter-log-events --log-group-name /aws/lambda/payload-enricher --start-time 1741046400000 --filter-pattern "ERROR" --limit 5`

```json
{
  "events": [
    {
      "timestamp": 1741128523456,
      "message": "[DEBUG] processing batch_id=4912 user_id=aud_2qKj item_count=187\n[DEBUG] fetching s3://customer-payloads-us-west-2/2026/03/03/aud_2qKj.json.gz size=842MB\n[DEBUG] decompressing payload aud_2qKj bytes=4.1GB\n[DEBUG] enriching with external_enrichment_api response_size=12.3KB",
      "logStreamName": "2026/03/03/[$LATEST]abcd1234"
    }
  ]
}
```

DEBUG logs dump 4GB+ decompressed payloads into the logs. Perfect storm.

---

## If Opus asks for something not listed here

You've got options:

- **Make something up** — say `[DEMO: no data]` or `(empty output)` or
  invent a plausible JSON response following the scenario. The exact
  values don't matter; what matters is whether Opus's reasoning tracks
  the breadcrumbs toward the root cause.
- **Type a free-text note** — e.g. `this Lambda was deployed recently,
  check its configuration` — Opus will adjust.
- **Skip it** with `/skip` — Opus will try another angle.
- **Tell Opus about the scenario** via `/note` — e.g.
  `/note this is a synthetic scenario; the spike started 2026-02-19`.

---

## Expected conclusion

After 4-6 commands Opus should conclude something like:

> **Root cause**: The `payload-enricher` Lambda deployed on 2026-02-19
> reads large objects (~800 MB each) from an S3 bucket in us-west-2 while
> running in us-east-1. With no S3 VPC endpoint, every read routes
> through NAT, billed at $0.045/GB processing + $0.09/GB inter-region
> transfer. Running at 3008 MB / 48s / 1440 invocations per day with
> DEBUG-level logging amplifies into Lambda GB-seconds, NAT data
> transfer, and CloudWatch Logs ingestion simultaneously.
>
> **Recommendations**: move the bucket or function so they're same-region;
> add an S3 gateway endpoint; set `LOG_LEVEL=INFO`; set a log-group
> retention policy; consider reducing memory if the function doesn't
> need 3008 MB.

"""MontyCloud performance-testing toolkit.

Layout:
  mcperf.core       — no Locust/boto3 imports: config, environments, credentials, redaction
  mcperf.locust     — Locust glue: base user, batches, sign-in/GET helpers, hooks
  mcperf.reporting  — custom HTML report built from Locust's CSV stats
  mcperf.api        — one flow module per scenario; mcperf.api.pages for reusable pages
  mcperf.runners    — sweeps (`python -m mcperf.runners.<name>`)
  mcperf.cfn        — CloudFormation resource generator (`python -m mcperf.cfn`)
"""

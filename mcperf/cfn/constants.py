"""
Shared constants: resource metadata, CloudFormation limits, stack-state sets.
Nothing here should import from other cfn.* modules.
"""

# Max resources per CFN stack — well below the 500-resource hard limit and sized
# to keep template bodies under the 51 KB inline limit.
BATCH_SIZE: dict[str, int] = {
    "sns_topic":             200,
    "sqs_queue":             200,
    "s3_bucket":             100,
    "lambda_function":       100,
    "dynamodb_table":        100,
    "auto_scaling_group":     50,
    "vpc":                    20,
    "subnet":                100,
    "security_group":        100,
    "route_table":           100,
    "internet_gateway":       50,
    "vpc_peering_connection": 50,
    "network_acl":           100,
    "target_group":          100,
    "iam_policy":             50,
    "iam_role":               50,
    "iam_user":               50,
}

RESOURCE_SHORT: dict[str, str] = {
    "sns_topic":             "sns",
    "sqs_queue":             "sqs",
    "s3_bucket":             "s3",
    "lambda_function":       "lambda",
    "dynamodb_table":        "ddb",
    "auto_scaling_group":    "asg",
    "vpc":                   "vpc",
    "subnet":                "subnet",
    "security_group":        "sg",
    "route_table":           "rtb",
    "internet_gateway":      "igw",
    "vpc_peering_connection":"pcx",
    "network_acl":           "nacl",
    "target_group":          "tg",
    "iam_policy":            "iampol",
    "iam_role":              "iamrole",
    "iam_user":              "iamuser",
}

SHORT_TO_RESOURCE: dict[str, str] = {v: k for k, v in RESOURCE_SHORT.items()}

# Resource types whose stacks require CAPABILITY_NAMED_IAM
NEEDS_NAMED_IAM: set[str] = {"iam_policy", "iam_role", "iam_user"}

S3_KEY_PREFIX = "cfn-templates"
INLINE_SIZE_LIMIT = 40_000   # bytes — conservative margin below CFN's 51,200 limit
MAX_WORKERS = 4              # parallel stack create/delete threads

# Live stacks — skip on re-run
HEALTHY_STATES = frozenset({
    "CREATE_COMPLETE",
    "CREATE_IN_PROGRESS",
    "UPDATE_COMPLETE",
    "UPDATE_IN_PROGRESS",
})

# Failed stacks — must be deleted before CloudFormation will accept a same-name create
FAILED_STATES = frozenset({
    "CREATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "UPDATE_ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_FAILED",
})

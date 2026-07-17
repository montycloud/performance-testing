"""
CloudFormation template generators.

Every public generator has the signature:
  gen_<resource_type>(prefix: str, start: int, count: int) -> dict

start is 1-based; resources are named {prefix}-{short}-{start:06d} through
{prefix}-{short}-{start+count-1:06d}.

TEMPLATE_GENERATORS maps resource-type keys to their generator functions.
"""


def _tmpl(resources: dict) -> dict:
    return {"AWSTemplateFormatVersion": "2010-09-09", "Resources": resources}


def gen_sns_topic(prefix: str, start: int, count: int) -> dict:
    return _tmpl({
        f"SNSTopic{i:06d}": {
            "Type": "AWS::SNS::Topic",
            "Properties": {"TopicName": f"{prefix}-sns-{i:06d}"},
        }
        for i in range(start, start + count)
    })


def gen_sqs_queue(prefix: str, start: int, count: int) -> dict:
    return _tmpl({
        f"SQSQueue{i:06d}": {
            "Type": "AWS::SQS::Queue",
            "Properties": {"QueueName": f"{prefix}-sqs-{i:06d}"},
        }
        for i in range(start, start + count)
    })


def gen_s3_bucket(prefix: str, start: int, count: int) -> dict:
    return _tmpl({
        f"S3Bucket{i:06d}": {
            "Type": "AWS::S3::Bucket",
            "Properties": {"BucketName": f"{prefix}-bucket-{i:06d}"},
        }
        for i in range(start, start + count)
    })


def gen_lambda_function(prefix: str, start: int, count: int) -> dict:
    return _tmpl({
        f"Lambda{i:06d}": {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "FunctionName": f"{prefix}-lambda-{i:06d}",
                "Runtime": "python3.12",
                "Handler": "index.handler",
                "Role": {"Fn::ImportValue": f"{prefix}-lambda-exec-role-arn"},
                "Code": {"ZipFile": "def handler(e, c): return 200"},
            },
        }
        for i in range(start, start + count)
    })


def gen_dynamodb_table(prefix: str, start: int, count: int) -> dict:
    return _tmpl({
        f"DDBTable{i:06d}": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "TableName": f"{prefix}-ddb-{i:06d}",
                "BillingMode": "PAY_PER_REQUEST",
                "AttributeDefinitions": [
                    {"AttributeName": "pk", "AttributeType": "S"}
                ],
                "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
            },
        }
        for i in range(start, start + count)
    })


def gen_iam_policy(prefix: str, start: int, count: int) -> dict:
    return _tmpl({
        f"IAMPolicy{i:06d}": {
            "Type": "AWS::IAM::ManagedPolicy",
            "Properties": {
                "ManagedPolicyName": f"{prefix}-policy-{i:06d}",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {"Effect": "Deny", "Action": "sts:AssumeRole", "Resource": "*"}
                    ],
                },
            },
        }
        for i in range(start, start + count)
    })


def gen_iam_role(prefix: str, start: int, count: int) -> dict:
    return _tmpl({
        f"IAMRole{i:06d}": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": f"{prefix}-role-{i:06d}",
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
            },
        }
        for i in range(start, start + count)
    })


def gen_iam_user(prefix: str, start: int, count: int) -> dict:
    return _tmpl({
        f"IAMUser{i:06d}": {
            "Type": "AWS::IAM::User",
            "Properties": {"UserName": f"{prefix}-user-{i:06d}"},
        }
        for i in range(start, start + count)
    })


TEMPLATE_GENERATORS: dict = {
    "sns_topic":       gen_sns_topic,
    "sqs_queue":       gen_sqs_queue,
    "s3_bucket":       gen_s3_bucket,
    "lambda_function": gen_lambda_function,
    "dynamodb_table":  gen_dynamodb_table,
    "iam_policy":      gen_iam_policy,
    "iam_role":        gen_iam_role,
    "iam_user":        gen_iam_user,
}


def lambda_roles_template(prefix: str) -> dict:
    """Standalone CFN template for the shared Lambda execution role stack."""
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {
            "LambdaExecRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": f"{prefix}-lambda-exec-role",
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "lambda.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                            }
                        ],
                    },
                    "ManagedPolicyArns": [
                        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                },
            }
        },
        "Outputs": {
            "LambdaExecRoleArn": {
                "Value": {"Fn::GetAtt": ["LambdaExecRole", "Arn"]},
                "Export": {"Name": f"{prefix}-lambda-exec-role-arn"},
            }
        },
    }

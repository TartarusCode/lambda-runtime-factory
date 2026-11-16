import json
from lambda_runtime_pypy import get_logger, subsegment


logger = get_logger("hello", service="sam-example")


def handler(event, context):
    with subsegment("format-response"):
        logger.info("handling request")
        return {"statusCode": 200, "body": json.dumps({"message": "hello world"})}

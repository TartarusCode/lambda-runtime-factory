const RUNTIME_API = process.env.AWS_LAMBDA_RUNTIME_API!;
const HANDLER = process.env._HANDLER!;
const BASE = `http://${RUNTIME_API}/2018-06-01/runtime`;

const [modulePath, funcName] = HANDLER.includes(".")
  ? [HANDLER.substring(0, HANDLER.lastIndexOf(".")), HANDLER.substring(HANDLER.lastIndexOf(".") + 1)]
  : [HANDLER, "handler"];

const mod = await import(`${process.env.LAMBDA_TASK_ROOT}/${modulePath}`);
const handlerFn = mod[funcName] ?? mod.default;
if (typeof handlerFn !== "function") {
  const body = JSON.stringify({ errorMessage: `Handler '${HANDLER}' is not a function`, errorType: "Runtime.HandlerNotFound" });
  await fetch(`${BASE}/init/error`, { method: "POST", body, headers: { "Content-Type": "application/json" } });
  process.exit(1);
}

while (true) {
  const next = await fetch(`${BASE}/invocation/next`);
  const requestId = next.headers.get("lambda-runtime-aws-request-id")!;
  const event = await next.json();

  const context = {
    awsRequestId: requestId,
    functionName: process.env.AWS_LAMBDA_FUNCTION_NAME,
    functionVersion: process.env.AWS_LAMBDA_FUNCTION_VERSION,
    invokedFunctionArn: next.headers.get("lambda-runtime-invoked-function-arn"),
    memoryLimitInMB: process.env.AWS_LAMBDA_FUNCTION_MEMORY_SIZE,
    logGroupName: process.env.AWS_LAMBDA_LOG_GROUP_NAME,
    logStreamName: process.env.AWS_LAMBDA_LOG_STREAM_NAME,
    getRemainingTimeInMillis: () => {
      const deadline = next.headers.get("lambda-runtime-deadline-ms");
      return deadline ? Math.max(0, Number(deadline) - Date.now()) : 0;
    },
  };

  try {
    const result = await handlerFn(event, context);
    const body = typeof result === "string" ? result : JSON.stringify(result);
    await fetch(`${BASE}/invocation/${requestId}/response`, { method: "POST", body, headers: { "Content-Type": "application/json" } });
  } catch (err: any) {
    const body = JSON.stringify({ errorMessage: err.message, errorType: err.name, stackTrace: err.stack?.split("\n") });
    await fetch(`${BASE}/invocation/${requestId}/error`, { method: "POST", body, headers: { "Content-Type": "application/json" } });
  }
}

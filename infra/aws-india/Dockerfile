FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}
COPY pyproject.toml ./
COPY src ./src
COPY web/dist ./web/dist
RUN pip install --no-cache-dir '.[app,aws]'
ENV CHURN_WEB_DIR=/var/task/web/dist

CMD ["churn_platform.lambda_handler.handler"]

# K8s 장애 대응 Copilot API 서버 이미지.
# 실행 예 (호스트의 kind 클러스터와 Prometheus에 접근해야 하므로 host 네트워크를 쓴다):
#   docker run --rm --network host --env-file .env \
#     -v "$PWD/.kube:/app/.kube:ro" -v "$PWD/.chroma:/app/.chroma" -v "$PWD/.checkpoints.db:/app/.checkpoints.db" \
#     k8s-copilot
# Slack 봇은 같은 이미지로 명령만 바꿔 실행한다: ... k8s-copilot python -m src.slack_app
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
# ragas는 Python 3.14에서 scikit-network 빌드가 실패하므로 의존성만 requirements.txt로 설치하고 본체는 --no-deps로 설치한다
RUN pip install -r requirements.txt && pip install --no-deps ragas

COPY src ./src
COPY config ./config
COPY data ./data
COPY evaluation ./evaluation

EXPOSE 8000

# 시크릿은 이미지에 넣지 않는다. --env-file로 .env를 전달한다
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]

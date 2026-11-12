
FROM python:3.11-slim


RUN apt-get update && apt-get install -y tar curl && rm -rf /var/lib/apt/lists/*


# Install docker-compose

RUN curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose && \

    chmod +x /usr/local/bin/docker-compose


WORKDIR /app


RUN pip install --no-cache-dir discord.py docker python-dotenv requests


COPY main.py .


# We DO NOT copy .env into the image; docker-compose will handle it


CMD ["python", "main.py"]

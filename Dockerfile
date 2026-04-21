FROM python:3.13-slim

# Set the working directory
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install build tools required for C-extension packages (e.g. psutil),
# then remove them to keep the final image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev libffi-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc python3-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create persistent data directory (SQLite DB lives here)
RUN mkdir -p /app/data

# Copy the source code
COPY src/ .

# Optional bootstrap env vars (override config.yaml values)
ENV EOS_WEB_PORT=""
ENV EOS_TIMEZONE=""
ENV EOS_LOG_LEVEL=""

# Expose the server port
EXPOSE 8081

# Command to run the application
CMD ["python", "eos_connect.py"]
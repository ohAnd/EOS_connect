FROM python:3.13-slim

# Build arguments for metadata
ARG VERSION
ARG COMMIT_SHA
ARG COMMIT_MESSAGE
ARG BUILD_DATE

# Set metadata labels
LABEL org.opencontainers.image.title="EOS Connect"
LABEL org.opencontainers.image.description="Open-source tool for intelligent energy management and optimization. ${COMMIT_MESSAGE}"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.revision="${COMMIT_SHA}"
LABEL org.opencontainers.image.created="${BUILD_DATE}"
LABEL org.opencontainers.image.source="https://github.com/ohAnd/EOS_connect"
LABEL org.opencontainers.image.url="https://github.com/ohAnd/EOS_connect"
LABEL org.opencontainers.image.documentation="https://github.com/ohAnd/EOS_connect#readme"
LABEL org.opencontainers.image.vendor="ohAnd"
LABEL org.opencontainers.image.licenses="MIT"

# Set the working directory
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install the dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the source code
COPY src/ .

# Expose the server port
EXPOSE 8081

# Command to run the application
CMD ["python", "eos_connect.py"]
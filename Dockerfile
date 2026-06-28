FROM alpine:latest

# Install system dependencies AND pre-compiled Python packages
RUN apk add --no-cache \
    supervisor \
    python3 \
    py3-pip \
    py3-psutil \
    py3-flask \
    py3-requests \
    curl \
    wget \
    jq \
    git \
    bash

# Install OpenJDK versions natively
RUN apk add --no-cache \
    openjdk8-jre \
    openjdk11-jre \
    openjdk17-jre \
    openjdk21-jre \
    openjdk25-jre 

WORKDIR /app
RUN mkdir -p /data /app/templates /app/static /var/log/supervisor /data/logs /data/branding

COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /entrypoint.sh
COPY app/ /app/
COPY version.txt /version.txt

# We only need pip for Flask-SocketIO now, which is pure Python and requires 0 compilation
RUN pip3 install --no-cache-dir --break-system-packages Flask-SocketIO==5.3.6 geoip2 maxminddb

RUN chmod +x /entrypoint.sh

EXPOSE 5000
EXPOSE 25565

ENTRYPOINT ["/entrypoint.sh"]
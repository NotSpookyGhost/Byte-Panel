FROM maven:3.9.9-eclipse-temurin-21 AS guard-build
WORKDIR /guard
COPY guard-plugin/pom.xml ./pom.xml
COPY guard-plugin/src ./src
RUN mvn -B -ntp package && sha512sum target/BytePanelGuard.jar | cut -d ' ' -f 1 > target/BytePanelGuard.sha512

FROM alpine:latest

# Install system dependencies AND pre-compiled Python packages
RUN apk add --no-cache \
    supervisor \
    python3 \
    py3-pip \
    py3-psutil \
    py3-flask \
    py3-requests \
    py3-pillow \
    py3-yaml \
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
COPY templates/ /templates/
COPY version.txt /version.txt
COPY --from=guard-build /guard/target/BytePanelGuard.jar /opt/bytepanel/guard/BytePanelGuard.jar
COPY --from=guard-build /guard/target/BytePanelGuard.sha512 /opt/bytepanel/guard/BytePanelGuard.sha512

# We only need pip for Flask-SocketIO now, which is pure Python and requires 0 compilation
RUN pip3 install --no-cache-dir --break-system-packages Flask-SocketIO==5.3.6 geoip2 maxminddb

RUN chmod +x /entrypoint.sh

EXPOSE 5000
EXPOSE 25565

ENTRYPOINT ["/entrypoint.sh"]

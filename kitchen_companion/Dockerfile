FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION=0.1.8
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache python3

COPY app /opt/kitchen/app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /opt/kitchen
EXPOSE 8099
CMD ["/run.sh"]

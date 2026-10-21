# Greengrass core image (linux/arm64). FROM the image built from the official
# aws-greengrass-docker Dockerfile at a pinned commit (see containers/buildspec.yml), plus the AWS
# CLI and a small entrypoint that fetches this core's certificate from Secrets Manager and writes
# the manual-provisioning config before handing over to the official entrypoint.
ARG BASE_IMAGE=greengrass-base:local
FROM ${BASE_IMAGE}
ARG NUCLEUS_VERSION
RUN yum install -y awscli-2 && yum clean all && rm -rf /var/cache/yum \
 && echo "${NUCLEUS_VERSION}" > /opt/greengrassv2/nucleus-version
COPY containers/greengrass/entrypoint.sh /edge-entrypoint.sh
RUN chmod 755 /edge-entrypoint.sh
ENTRYPOINT ["/edge-entrypoint.sh"]

# Greengrass core image (linux/arm64). FROM the image built from the official
# aws-greengrass-docker Dockerfile at a pinned commit (see containers/buildspec.yml), plus the AWS
# CLI and a small entrypoint that fetches this core's certificate from Secrets Manager and writes
# the manual-provisioning config before handing over to the official entrypoint.
# The base is built in the same CodeBuild run and tagged with the pinned nucleus version.
ARG BASE_IMAGE=greengrass-base:2.17.0
FROM ${BASE_IMAGE}
# checkov:skip=CKV_DOCKER_3: The official aws-greengrass-docker image runs the nucleus as root (its Dockerfile has no USER and edits sudoers) so that the nucleus can run each component as the non-root ggc_user; components run as ggc_user (COMPONENT_DEFAULT_USER).
# checkov:skip=CKV_DOCKER_7: The base is a local tag produced from the pinned official Dockerfile commit in the same build (containers/buildspec.yml), not a registry image.
ARG NUCLEUS_VERSION
# Default component user (found in testing: the nucleus reported "ggc_user: no such user").
RUN yum install -y awscli-2 && yum clean all && rm -rf /var/cache/yum \
 && groupadd --system ggc_group && useradd --system --gid ggc_group --no-create-home ggc_user \
 && echo "${NUCLEUS_VERSION}" > /opt/greengrassv2/nucleus-version \
 && install -d -m 0700 /opt/edge-config
COPY containers/greengrass/entrypoint.sh /edge-entrypoint.sh
RUN chmod 755 /edge-entrypoint.sh
# Healthy while the nucleus JVM is running (pgrep comes with procps, installed by the official image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 CMD pgrep -f Greengrass.jar >/dev/null || exit 1
ENTRYPOINT ["/edge-entrypoint.sh"]

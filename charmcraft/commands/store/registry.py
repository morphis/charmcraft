# Copyright 2021 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# For further info, check https://github.com/canonical/charmcraft

"""Module to work with OCI registries."""

import base64
import logging
import urllib.parse
from urllib.request import parse_http_list, parse_keqv_list

import requests

from charmcraft.cmdbase import CommandError

logger = logging.getLogger(__name__)

# some mimetypes
MANIFEST_LISTS = "application/vnd.docker.distribution.manifest.list.v2+json"
MANIFEST_V2_MIMETYPE = "application/vnd.docker.distribution.manifest.v2+json"
LAYER_MIMETYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"
JSON_RELATED_MIMETYPES = {
    "application/json",
    "application/vnd.docker.distribution.manifest.v1+prettyjws",  # signed manifest
    MANIFEST_LISTS,
    MANIFEST_V2_MIMETYPE,
}
OCTET_STREAM_MIMETYPE = "application/octet-stream"

# downloads and uploads happen in chunks
CHUNK_SIZE = 2 ** 20


def assert_response_ok(response, expected_status=200):
    """Assert the response is ok."""
    if response.status_code != expected_status:
        if response.headers.get("Content-Type") in JSON_RELATED_MIMETYPES:
            errors = response.json().get("errors")
        else:
            errors = None
        raise CommandError(
            "Wrong status code from server (expected={}, got={}) errors={} headers={}".format(
                expected_status, response.status_code, errors, response.headers
            )
        )

    if response.headers.get("Content-Type") not in JSON_RELATED_MIMETYPES:
        return

    result = response.json()
    if "errors" in result:
        raise CommandError(
            "Response with errors from server: {}".format(result["errors"])
        )
    return result


class OCIRegistry:
    """Interface to a generic OCI Registry."""

    def __init__(self, server, image_name, *, username="", password=""):
        self.server = server
        self.image_name = image_name
        self.auth_token = None

        if username:
            _u_p = "{}:{}".format(username, password)
            self.auth_encoded_credentials = base64.b64encode(
                _u_p.encode("ascii")
            ).decode("ascii")
        else:
            self.auth_encoded_credentials = None

    def __eq__(self, other):
        return (
            self.server == other.server
            and self.image_name == other.image_name
            and self.auth_encoded_credentials == other.auth_encoded_credentials
        )

    def _authenticate(self, auth_info):
        """Get the auth token."""
        headers = {}
        if self.auth_encoded_credentials is not None:
            headers["Authorization"] = "Basic {}".format(self.auth_encoded_credentials)

        logger.debug("Authenticating! %s", auth_info)
        url = "{realm}?service={service}&scope={scope}".format_map(auth_info)
        response = requests.get(url, headers=headers)

        result = assert_response_ok(response)
        auth_token = result["token"]
        return auth_token

    def _get_url(self, subpath):
        """Build the URL completing the subpath."""
        return "{}/v2/{}/{}".format(self.server, self.image_name, subpath)

    def _get_auth_info(self, response):
        """Parse a 401 response and get the needed auth parameters."""
        www_auth = response.headers["Www-Authenticate"]
        if not www_auth.startswith("Bearer "):
            raise ValueError("Bearer not found")
        info = parse_keqv_list(parse_http_list(www_auth[7:]))
        return info

    def _hit(self, method, url, headers=None, log=True, **kwargs):
        """Hit the specific URL, taking care of the authentication."""
        if headers is None:
            headers = {}
        if self.auth_token is not None:
            headers["Authorization"] = "Bearer {}".format(self.auth_token)

        if log:
            logger.debug("Hitting the registry: %s %s", method, url)
        response = requests.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            # token expired or missing, let's get another one and retry
            try:
                auth_info = self._get_auth_info(response)
            except (ValueError, KeyError) as exc:
                raise CommandError(
                    "Bad 401 response: {}; headers: {!r}".format(exc, response.headers)
                )
            self.auth_token = self._authenticate(auth_info)
            headers["Authorization"] = "Bearer {}".format(self.auth_token)
            response = requests.request(method, url, headers=headers, **kwargs)

        return response

    def get_fully_qualified_url(self, digest):
        """Return the fully qualified URL univocally specifying the element in the registry."""
        netloc = urllib.parse.urlparse(self.server).netloc
        return "{}/{}@{}".format(netloc, self.image_name, digest)

    def _is_item_already_uploaded(self, url):
        """Verify if a generic item is uploaded."""
        response = self._hit("HEAD", url)

        if response.status_code == 200:
            # item is there, done!
            uploaded = True
        elif response.status_code == 404:
            # confirmed item is NOT there
            uploaded = False
        else:
            # something else is going on, log what we have and return False so at least
            # we can continue with the upload
            logger.debug(
                "Bad response when checking for uploaded %r: %r (headers=%s)",
                url,
                response.status_code,
                response.headers,
            )
            uploaded = False
        return uploaded

    def is_manifest_already_uploaded(self, reference):
        """Verify if the manifest is already uploaded, using a generic reference.

        If yes, return its digest.
        """
        logger.debug("Checking if manifest is already uploaded")
        url = self._get_url("manifests/{}".format(reference))
        return self._is_item_already_uploaded(url)

    def is_blob_already_uploaded(self, reference):
        """Verify if the blob is already uploaded, using a generic reference.

        If yes, return its digest.
        """
        logger.debug("Checking if the blob is already uploaded")
        url = self._get_url("blobs/{}".format(reference))
        return self._is_item_already_uploaded(url)

    def upload_manifest(self, manifest_data, reference):
        """Upload a manifest."""
        url = self._get_url("manifests/{}".format(reference))
        headers = {
            "Content-Type": MANIFEST_V2_MIMETYPE,
        }
        logger.debug("Uploading manifest with reference %s", reference)
        response = self._hit(
            "PUT", url, headers=headers, data=manifest_data.encode("utf8")
        )
        assert_response_ok(response, expected_status=201)
        logger.debug("Manifest uploaded OK")

    def upload_blob(self, filepath, size, digest):
        """Upload the blob from a file."""
        # get the first URL to start pushing the blob
        logger.debug("Getting URL to push the blob")
        url = self._get_url("blobs/uploads/")
        response = self._hit("POST", url)
        assert_response_ok(response, expected_status=202)
        upload_url = response.headers["Location"]
        range_from, range_to_inclusive = [
            int(x) for x in response.headers["Range"].split("-")
        ]
        logger.debug(
            "Got upload URL ok with range %s-%s", range_from, range_to_inclusive
        )
        if range_from != 0:
            raise CommandError("Server error: bad range received")
        if range_to_inclusive == 0:
            range_to_inclusive = -1

        # start the chunked upload
        from_position = range_to_inclusive + 1
        with open(filepath, "rb") as fh:
            fh.seek(from_position)
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break

                end_position = from_position + len(chunk)
                headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": "{}-{}".format(from_position, end_position),
                    "Content-Type": OCTET_STREAM_MIMETYPE,
                }
                progress = 100 * end_position / size
                print("Uploading.. {:.2f}%\r".format(progress), end="", flush=True)
                response = self._hit(
                    "PATCH", upload_url, headers=headers, data=chunk, log=False
                )
                assert_response_ok(response, expected_status=202)

                upload_url = response.headers["Location"]
                from_position += len(chunk)

        headers = {
            "Content-Length": "0",
            "Connection": "close",
        }
        logger.debug("Closing the upload")
        closing_url = "{}&digest={}".format(upload_url, digest)

        response = self._hit("PUT", closing_url, headers=headers, data="")
        assert_response_ok(response, expected_status=201)
        logger.debug("Upload finished OK")
        if response.headers["Docker-Content-Digest"] != digest:
            raise CommandError("Server error: the upload is corrupted")

    def get_manifest(self, reference):
        """Get the manifest for the indicated reference."""
        url = self._get_url("manifests/{}".format(reference))
        logger.debug("Getting manifests list for %s", reference)
        headers = {
            "Accept": MANIFEST_LISTS,
        }
        response = self._hit("GET", url, headers=headers)
        result = assert_response_ok(response)
        digest = response.headers["Docker-Content-Digest"]

        # the response can be the manifest itself or a list of manifests (only determined
        # by the presence of the 'manifests' key
        manifests = result.get("manifests")

        if manifests is not None:
            return (manifests, digest, response.text)

        logger.debug("Got the manifest directly, schema %s", result["schemaVersion"])
        if result["schemaVersion"] != 2:
            # get the manifest in v2! cannot request it directly, as that will avoid us
            # getting the manifests list when available
            headers = {
                "Accept": MANIFEST_V2_MIMETYPE,
            }
            response = self._hit("GET", url, headers=headers)
            result = assert_response_ok(response)
            if result.get("schemaVersion") != 2:
                logger.debug(
                    "Got something else when asking for a v2 manifest: %s", result
                )
                raise CommandError("Manifest v2 not found for {!r}.".format(reference))
            logger.debug("Got the v2 manifest ok")
            digest = response.headers["Docker-Content-Digest"]
        return (None, digest, response.text)


class ImageHandler:
    """Provide specific functionalities around images."""

    def __init__(self, registry):
        self.registry = registry

    def get_destination_url(self, reference):
        """Get the fully qualified URL in the destination registry for a tag/digest reference."""
        if not self.registry.is_manifest_already_uploaded(reference):
            raise CommandError(
                "The {!r} image does not exist in the destination registry".format(
                    reference
                )
            )

        # need to actually get the manifest, because this is what we'll end up getting the v2 one
        _, digest, _ = self.registry.get_manifest(reference)
        final_fqu = self.registry.get_fully_qualified_url(digest)
        return final_fqu

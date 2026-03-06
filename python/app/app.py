import json
import socketserver
import threading
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

from kubernetes import client
from kubernetes.client.rest import ApiException


# Calico CRD details for this cluster
CALICO_GROUP = "crd.projectcalico.org"
CALICO_VERSION = "v1"
CALICO_PLURAL = "networkpolicies"
CALICO_KIND = "NetworkPolicy"

# Used to identify policies created by this tool
MANAGED_BY_LABEL_KEY = "managed-by"
MANAGED_BY_LABEL_VALUE = "tyk-sre-tool"
BLOCK_NAME_LABEL_KEY = "block-name"

# This helps /readyz fail during shutdown
SHUTTING_DOWN = False


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Handle incoming GET requests."""
        parsed = urlparse(self.path)

        if parsed.path == "/healthz":
            self.healthz()
        elif parsed.path == "/readyz":
            self.readyz()
        elif parsed.path == "/deployments/health":
            self.deployments_health()
        elif parsed.path == "/networkpolicies":
            self.list_network_policies(parsed.query)
        else:
            self.send_error(404)

    def do_POST(self):
        """Handle incoming POST requests."""
        parsed = urlparse(self.path)

        if parsed.path == "/networkpolicies":
            self.create_network_policies()
        else:
            self.send_error(404)

    def do_DELETE(self):
        """Handle incoming DELETE requests."""
        parsed = urlparse(self.path)

        if parsed.path.startswith("/networkpolicies/"):
            block_name = parsed.path.rsplit("/", 1)[-1]
            self.delete_network_policies(block_name)
        else:
            self.send_error(404)

    def healthz(self):
        """Simple health check. This is kept unchanged for the existing tests."""
        self.respond_text(200, "ok")

    def readyz(self):
        """Readiness check. Fail it when shutdown has started."""
        if SHUTTING_DOWN:
            self.respond_text(503, "shutting down")
            return

        try:
            api_client = client.ApiClient()
            get_kubernetes_version(api_client)
            self.respond_text(200, "ready")
        except Exception as exc:
            self.respond_json(503, {"ready": False, "error": str(exc)})

    def deployments_health(self):
        """Return whether deployments have the requested healthy replicas."""
        try:
            result = get_deployments_health()
            self.respond_json(200, result)
        except Exception as exc:
            self.respond_json(500, {"error": str(exc)})

    def list_network_policies(self, query_string):
        """List Calico network policies. Can filter only tool-managed ones."""
        try:
            params = parse_qs(query_string)
            managed_only = params.get("managed_only", ["false"])[0].lower() == "true"

            result = list_calico_network_policies(managed_only=managed_only)
            self.respond_json(200, result)
        except Exception as exc:
            self.respond_json(500, {"error": str(exc)})

    def create_network_policies(self):
        """
        Create a bidirectional block between two workloads.

        Expected JSON:
        {
          "name": "team-a-to-team-b",
          "sourceNamespace": "team-a",
          "sourceLabels": {"app": "api-a"},
          "targetNamespace": "team-b",
          "targetLabels": {"app": "api-b"}
        }
        """
        try:
            body = self.read_json_body()

            validate_create_request(body)

            block_name = body["name"]
            source_namespace = body["sourceNamespace"]
            source_labels = body["sourceLabels"]
            target_namespace = body["targetNamespace"]
            target_labels = body["targetLabels"]

            source_selector = labels_to_calico_selector(source_labels)
            target_selector = labels_to_calico_selector(target_labels)

            # One policy in source namespace denying traffic to/from target
            source_policy_name = f"{block_name}-source"
            source_policy = build_bidirectional_block_policy(
                name=source_policy_name,
                namespace=source_namespace,
                local_selector=source_selector,
                remote_namespace=target_namespace,
                remote_selector=target_selector,
                block_name=block_name,
            )

            # One policy in target namespace denying traffic to/from source
            target_policy_name = f"{block_name}-target"
            target_policy = build_bidirectional_block_policy(
                name=target_policy_name,
                namespace=target_namespace,
                local_selector=target_selector,
                remote_namespace=source_namespace,
                remote_selector=source_selector,
                block_name=block_name,
            )

            create_calico_network_policy(source_namespace, source_policy)
            create_calico_network_policy(target_namespace, target_policy)

            self.respond_json(
                201,
                {
                    "message": "bidirectional block created",
                    "blockName": block_name,
                    "policies": [
                        {"name": source_policy_name, "namespace": source_namespace},
                        {"name": target_policy_name, "namespace": target_namespace},
                    ],
                },
            )
        except ValueError as exc:
            self.respond_json(400, {"error": str(exc)})
        except ApiException as exc:
            self.respond_json(500, {"error": format_api_exception(exc)})
        except Exception as exc:
            self.respond_json(500, {"error": str(exc)})

    def delete_network_policies(self, block_name):
        """Delete both policies created for a block name."""
        try:
            deleted = delete_managed_block_policies(block_name)
            self.respond_json(
                200,
                {
                    "message": "delete completed",
                    "blockName": block_name,
                    "deleted": deleted,
                },
            )
        except ApiException as exc:
            self.respond_json(500, {"error": format_api_exception(exc)})
        except Exception as exc:
            self.respond_json(500, {"error": str(exc)})

    def read_json_body(self):
        """Read JSON from the request body."""
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")

        if not raw_body:
            raise ValueError("request body is empty")

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            raise ValueError("request body must be valid JSON")

    def respond_text(self, status: int, content: str):
        """Return plain text."""
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(bytes(content, "utf-8"))

    def respond_json(self, status: int, content):
        """Return JSON response."""
        body = json.dumps(content, indent=2)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(bytes(body, "utf-8"))


def get_kubernetes_version(api_client: client.ApiClient) -> str:
    """Get the Kubernetes version from the API server."""
    version = client.VersionApi(api_client).get_code()
    return version.git_version


def get_deployments_health():
    """
    Check whether deployments have as many healthy pods as requested.

    We use ready_replicas as the simple signal for healthy replicas.
    """
    apps_api = client.AppsV1Api()
    deployments = apps_api.list_deployment_for_all_namespaces().items

    items = []
    all_healthy = True

    for deployment in deployments:
        requested = deployment.spec.replicas or 0
        ready = deployment.status.ready_replicas or 0
        healthy = ready == requested

        if not healthy:
            all_healthy = False

        items.append(
            {
                "namespace": deployment.metadata.namespace,
                "name": deployment.metadata.name,
                "requestedReplicas": requested,
                "readyReplicas": ready,
                "healthy": healthy,
            }
        )

    return {
        "healthy": all_healthy,
        "deployments": items,
    }


def labels_to_calico_selector(labels):
    """
    Turn a simple label map into a Calico selector string.

    Example:
    {"app": "api-a", "tier": "backend"}
    becomes
    "app == 'api-a' && tier == 'backend'"
    """
    if not isinstance(labels, dict) or not labels:
        raise ValueError("labels must be a non-empty object")

    parts = []

    for key in sorted(labels.keys()):
        value = labels[key]

        if not isinstance(key, str) or not key.strip():
            raise ValueError("label keys must be non-empty strings")

        if not isinstance(value, str) or not value.strip():
            raise ValueError("label values must be non-empty strings")

        safe_value = value.replace("'", "\\'")
        parts.append(f"{key} == '{safe_value}'")

    return " && ".join(parts)


def build_namespace_selector(namespace):
    """
    Build a namespace selector for Calico.

    This uses the common Calico namespace label.
    """
    safe_namespace = namespace.replace("'", "\\'")
    return f"projectcalico.org/name == '{safe_namespace}'"


def build_bidirectional_block_policy(
    name,
    namespace,
    local_selector,
    remote_namespace,
    remote_selector,
    block_name,
):
    """
    Build one Calico policy.

    This policy applies to local pods in one namespace and denies traffic
    to and from the remote workload.
    """
    return {
        "apiVersion": f"{CALICO_GROUP}/{CALICO_VERSION}",
        "kind": CALICO_KIND,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                MANAGED_BY_LABEL_KEY: MANAGED_BY_LABEL_VALUE,
                BLOCK_NAME_LABEL_KEY: block_name,
            },
        },
        "spec": {
            "selector": local_selector,
            "types": ["Ingress", "Egress"],
            "ingress": [
                {
                    "action": "Deny",
                    "source": {
                        "namespaceSelector": build_namespace_selector(remote_namespace),
                        "selector": remote_selector,
                    },
                }
            ],
            "egress": [
                {
                    "action": "Deny",
                    "destination": {
                        "namespaceSelector": build_namespace_selector(remote_namespace),
                        "selector": remote_selector,
                    },
                }
            ],
        },
    }


def get_custom_objects_api():
    """Create a CustomObjectsApi client."""
    return client.CustomObjectsApi()


def create_calico_network_policy(namespace, body):
    """Create one Calico NetworkPolicy in a namespace."""
    custom_api = get_custom_objects_api()

    return custom_api.create_namespaced_custom_object(
        group=CALICO_GROUP,
        version=CALICO_VERSION,
        namespace=namespace,
        plural=CALICO_PLURAL,
        body=body,
    )


def list_calico_network_policies(managed_only=False):
    """List Calico network policies across the cluster."""
    custom_api = get_custom_objects_api()

    result = custom_api.list_cluster_custom_object(
        group=CALICO_GROUP,
        version=CALICO_VERSION,
        plural=CALICO_PLURAL,
    )

    items = result.get("items", [])
    response_items = []

    for item in items:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})

        managed = labels.get(MANAGED_BY_LABEL_KEY) == MANAGED_BY_LABEL_VALUE
        if managed_only and not managed:
            continue

        response_items.append(
            {
                "name": metadata.get("name"),
                "namespace": metadata.get("namespace"),
                "managed": managed,
                "blockName": labels.get(BLOCK_NAME_LABEL_KEY),
            }
        )

    response_items.sort(key=lambda x: (x.get("namespace") or "", x.get("name") or ""))

    return {
        "items": response_items,
        "count": len(response_items),
    }


def delete_managed_block_policies(block_name):
    """Delete policies created by this tool for one block name."""
    custom_api = get_custom_objects_api()
    all_items = list_calico_network_policies(managed_only=True)["items"]

    deleted = []

    for item in all_items:
        if item.get("blockName") != block_name:
            continue

        namespace = item["namespace"]
        name = item["name"]

        custom_api.delete_namespaced_custom_object(
            group=CALICO_GROUP,
            version=CALICO_VERSION,
            namespace=namespace,
            plural=CALICO_PLURAL,
            name=name,
        )

        deleted.append({"name": name, "namespace": namespace})

    return deleted


def validate_create_request(body):
    """Validate the POST payload for creating a block."""
    required_fields = [
        "name",
        "sourceNamespace",
        "sourceLabels",
        "targetNamespace",
        "targetLabels",
    ]

    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")

    for field in required_fields:
        if field not in body:
            raise ValueError(f"missing required field: {field}")

    if not isinstance(body["name"], str) or not body["name"].strip():
        raise ValueError("name must be a non-empty string")

    if not isinstance(body["sourceNamespace"], str) or not body["sourceNamespace"].strip():
        raise ValueError("sourceNamespace must be a non-empty string")

    if not isinstance(body["targetNamespace"], str) or not body["targetNamespace"].strip():
        raise ValueError("targetNamespace must be a non-empty string")

    if body["sourceNamespace"] == body["targetNamespace"]:
        raise ValueError("sourceNamespace and targetNamespace must be different")

    labels_to_calico_selector(body["sourceLabels"])
    labels_to_calico_selector(body["targetLabels"])


def format_api_exception(exc):
    """Return a cleaner API error message."""
    return f"{exc.status} {exc.reason}: {exc.body}"


def start_server(address):
    """
    Start the HTTP server.

    Address should look like:
    - :8080
    - 0.0.0.0:8080
    - 127.0.0.1:8080
    """
    try:
        host, port = address.split(":")
        port = int(port)
    except ValueError:
        print("invalid server address format")
        return

    class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with ReusableThreadingTCPServer((host, port), AppHandler) as httpd:
        print(f"Server listening on {address}")
        httpd.serve_forever()


def set_shutting_down():
    """Mark the app as shutting down so readiness fails."""
    global SHUTTING_DOWN
    SHUTTING_DOWN = True

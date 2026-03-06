import json
import re
import socketserver
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from kubernetes import client

APP_NAME = "tyk-sre-tool"
APP_VERSION = "1.0.0"
CALICO_GROUP = "crd.projectcalico.org"
CALICO_VERSION = "v1"
CALICO_PLURAL = "networkpolicies"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = APP_NAME
BLOCK_GROUP_LABEL = "tyk.io/block-group"

API_CLIENT = None


def configure_kubernetes(api_client: client.ApiClient):
    """Store a configured Kubernetes API client for request handlers."""
    global API_CLIENT
    API_CLIENT = api_client


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Handle GET endpoints."""
        parsed = urlparse(self.path)

        if parsed.path == "/healthz":
            self.respond_text(200, "ok")
            return

        if parsed.path == "/readyz":
            self.handle_readyz()
            return

        if parsed.path == "/deployments/health":
            self.handle_deployments_health()
            return

        if parsed.path == "/networkpolicies":
            params = parse_qs(parsed.query)
            managed_only = params.get("managed_only", ["false"])[0].lower() == "true"
            self.handle_list_network_policies(managed_only)
            return

        self.send_error(404)

    def do_POST(self):
        """Handle POST endpoints."""
        parsed = urlparse(self.path)

        if parsed.path == "/networkpolicies":
            self.handle_create_network_policy()
            return

        self.send_error(404)

    def do_DELETE(self):
        """Handle DELETE endpoints."""
        parsed = urlparse(self.path)

        if parsed.path.startswith("/networkpolicies/"):
            block_group = parsed.path.split("/")[-1].strip()
            self.handle_delete_network_policy(block_group)
            return

        self.send_error(404)

    def handle_readyz(self):
        """Return API server readiness by calling the Kubernetes version endpoint."""
        try:
            version = get_kubernetes_version(get_api_client())
            self.respond_json(200, {"status": "ok", "kubernetesVersion": version})
        except Exception as exc:
            self.respond_json(503, {"status": "error", "error": str(exc)})

    def handle_deployments_health(self):
        """List deployment health for all namespaces."""
        try:
            data = get_deployments_health()
            status_code = 200 if data["unhealthyDeployments"] == 0 else 503
            self.respond_json(status_code, data)
        except Exception as exc:
            self.respond_json(500, {"status": "error", "error": str(exc)})

    def handle_list_network_policies(self, managed_only=False):
        """List Calico network policies."""
        try:
            items = list_network_policies(managed_only=managed_only)
            self.respond_json(200, {"count": len(items), "items": items})
        except Exception as exc:
            self.respond_json(500, {"status": "error", "error": str(exc)})

    def handle_create_network_policy(self):
        """Create a bidirectional deny policy pair."""
        try:
            payload = read_json_body(self)
            validate_create_request(payload)
            result = create_bidirectional_block(payload)
            self.respond_json(201, result)
        except ValueError as exc:
            self.respond_json(400, {"status": "error", "error": str(exc)})
        except Exception as exc:
            self.respond_json(500, {"status": "error", "error": str(exc)})

    def handle_delete_network_policy(self, block_group):
        """Delete the managed policy pair by block group name."""
        try:
            if not block_group:
                raise ValueError("block group name is required")
            result = delete_bidirectional_block(block_group)
            self.respond_json(200, result)
        except ValueError as exc:
            self.respond_json(400, {"status": "error", "error": str(exc)})
        except Exception as exc:
            self.respond_json(500, {"status": "error", "error": str(exc)})

    def respond_text(self, status: int, content: str):
        """Write a plain text response."""
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def respond_json(self, status: int, payload):
        """Write a JSON response."""
        body = json.dumps(payload, indent=2, sort_keys=True)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        """Keep local runs quieter."""
        return


def get_api_client() -> client.ApiClient:
    """Return the configured Kubernetes API client."""
    if API_CLIENT is None:
        raise RuntimeError("kubernetes client is not configured")
    return API_CLIENT



def get_kubernetes_version(api_client: client.ApiClient) -> str:
    """Return the Kubernetes git version string."""
    version = client.VersionApi(api_client).get_code()
    return version.git_version



def get_deployments_health():
    """Return health information for deployments across the cluster."""
    apps_api = client.AppsV1Api(get_api_client())
    deployments = apps_api.list_deployment_for_all_namespaces().items

    items = []
    unhealthy = 0

    for deployment in deployments:
        requested = deployment.spec.replicas or 0
        ready = deployment.status.ready_replicas or 0
        available = deployment.status.available_replicas or 0
        healthy = ready >= requested and available >= requested

        if not healthy:
            unhealthy += 1

        items.append(
            {
                "namespace": deployment.metadata.namespace,
                "name": deployment.metadata.name,
                "requestedReplicas": requested,
                "readyReplicas": ready,
                "availableReplicas": available,
                "healthy": healthy,
            }
        )

    return {
        "status": "ok" if unhealthy == 0 else "degraded",
        "totalDeployments": len(items),
        "unhealthyDeployments": unhealthy,
        "items": items,
    }



def list_network_policies(managed_only=False):
    """List Calico network policies from the cluster."""
    custom_api = client.CustomObjectsApi(get_api_client())
    response = custom_api.list_cluster_custom_object(
        group=CALICO_GROUP,
        version=CALICO_VERSION,
        plural=CALICO_PLURAL,
    )

    items = []
    for item in response.get("items", []):
        labels = item.get("metadata", {}).get("labels", {})
        if managed_only and labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
            continue

        items.append(
            {
                "namespace": item.get("metadata", {}).get("namespace"),
                "name": item.get("metadata", {}).get("name"),
                "managed": labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE,
                "blockGroup": labels.get(BLOCK_GROUP_LABEL, ""),
                "selector": item.get("spec", {}).get("selector", ""),
                "types": item.get("spec", {}).get("types", []),
            }
        )

    return sorted(items, key=lambda x: ((x["namespace"] or ""), x["name"] or ""))



def create_bidirectional_block(payload):
    """Create two Calico policies so traffic is blocked in both directions."""
    block_group = sanitize_name(payload["name"])
    source_namespace = payload["sourceNamespace"]
    target_namespace = payload["targetNamespace"]
    source_selector = payload["sourceSelector"]
    target_selector = payload["targetSelector"]

    custom_api = client.CustomObjectsApi(get_api_client())

    source_policy_name = f"{block_group}-source"
    target_policy_name = f"{block_group}-target"

    source_body = build_policy_body(
        namespace=source_namespace,
        policy_name=source_policy_name,
        block_group=block_group,
        local_selector=source_selector,
        remote_namespace=target_namespace,
        remote_selector=target_selector,
    )
    target_body = build_policy_body(
        namespace=target_namespace,
        policy_name=target_policy_name,
        block_group=block_group,
        local_selector=target_selector,
        remote_namespace=source_namespace,
        remote_selector=source_selector,
    )

    created = []
    try:
        created.append(
            custom_api.create_namespaced_custom_object(
                group=CALICO_GROUP,
                version=CALICO_VERSION,
                namespace=source_namespace,
                plural=CALICO_PLURAL,
                body=source_body,
            )
        )
        created.append(
            custom_api.create_namespaced_custom_object(
                group=CALICO_GROUP,
                version=CALICO_VERSION,
                namespace=target_namespace,
                plural=CALICO_PLURAL,
                body=target_body,
            )
        )
    except Exception:
        for item in created:
            metadata = item.get("metadata", {})
            try:
                custom_api.delete_namespaced_custom_object(
                    group=CALICO_GROUP,
                    version=CALICO_VERSION,
                    namespace=metadata.get("namespace"),
                    plural=CALICO_PLURAL,
                    name=metadata.get("name"),
                )
            except Exception:
                pass
        raise

    return {
        "status": "created",
        "blockGroup": block_group,
        "policies": [
            {"namespace": source_namespace, "name": source_policy_name},
            {"namespace": target_namespace, "name": target_policy_name},
        ],
    }



def delete_bidirectional_block(block_group):
    """Delete all managed policies that belong to one block group."""
    custom_api = client.CustomObjectsApi(get_api_client())
    items = list_network_policies(managed_only=True)

    deleted = []
    for item in items:
        if item["blockGroup"] != block_group:
            continue

        custom_api.delete_namespaced_custom_object(
            group=CALICO_GROUP,
            version=CALICO_VERSION,
            namespace=item["namespace"],
            plural=CALICO_PLURAL,
            name=item["name"],
        )
        deleted.append({"namespace": item["namespace"], "name": item["name"]})

    if not deleted:
        raise ValueError(f"no managed policies found for block group '{block_group}'")

    return {"status": "deleted", "blockGroup": block_group, "policies": deleted}



def build_policy_body(namespace, policy_name, block_group, local_selector, remote_namespace, remote_selector):
    """Build one Calico namespaced policy with deny ingress and egress rules."""
    return {
        "apiVersion": f"{CALICO_GROUP}/{CALICO_VERSION}",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": policy_name,
            "namespace": namespace,
            "labels": {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                BLOCK_GROUP_LABEL: block_group,
            },
        },
        "spec": {
            "order": 100.0,
            "selector": local_selector,
            "types": ["Ingress", "Egress"],
            "ingress": [
                {
                    "action": "Deny",
                    "source": {
                        "namespaceSelector": f"projectcalico.org/name == '{remote_namespace}'",
                        "selector": remote_selector,
                    },
                }
            ],
            "egress": [
                {
                    "action": "Deny",
                    "destination": {
                        "namespaceSelector": f"projectcalico.org/name == '{remote_namespace}'",
                        "selector": remote_selector,
                    },
                }
            ],
        },
    }



def validate_create_request(payload):
    """Check the required request fields."""
    required = ["name", "sourceNamespace", "sourceSelector", "targetNamespace", "targetSelector"]
    for key in required:
        if key not in payload or not str(payload[key]).strip():
            raise ValueError(f"'{key}' is required")



def read_json_body(handler):
    """Read and parse a JSON request body."""
    content_length = int(handler.headers.get("Content-Length", 0))
    if content_length <= 0:
        raise ValueError("request body is required")

    raw_body = handler.rfile.read(content_length).decode("utf-8")
    try:
        return json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc



def sanitize_name(value):
    """Convert a free-form name into a Kubernetes-friendly object name."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9-]", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    if not value:
        raise ValueError("name must contain letters or numbers")
    return value[:50]



def start_server(address):
    """Start the HTTP server and block until interrupted."""
    try:
        host, port = address.split(":", 1)
    except ValueError:
        print("invalid server address format, use host:port")
        return

    server_class = socketserver.ThreadingTCPServer
    server_class.allow_reuse_address = True

    with server_class((host, int(port)), AppHandler) as httpd:
        print(f"Server listening on {address}")
        httpd.serve_forever()


import json
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kubernetes import client
from kubernetes.client.rest import ApiException


# Simple shared state so the request handler can use the k8s clients.
STATE = {
    "api_client": None,
    "core_api": None,
    "apps_api": None,
    "custom_api": None,
}

SERVER = None

CALICO_GROUP = "crd.projectcalico.org"
CALICO_VERSION = "v1"
CALICO_PLURAL = "networkpolicies"

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "sre-tool"
POLICY_GROUP_LABEL = "sre-tool/policy-group"


def configure_kubernetes(api_client: client.ApiClient):
    """Store Kubernetes clients used by the HTTP handlers."""
    STATE["api_client"] = api_client
    STATE["core_api"] = client.CoreV1Api(api_client)
    STATE["apps_api"] = client.AppsV1Api(api_client)
    STATE["custom_api"] = client.CustomObjectsApi(api_client)


def get_kubernetes_version(api_client: client.ApiClient) -> str:
    """
    Returns the GitVersion of the Kubernetes server.

    Raises the original exception if the API server cannot be reached.
    """
    version = client.VersionApi(api_client).get_code()
    return version.git_version


def _build_selector(labels: dict) -> str:
    """Convert a dict like {'app': 'api'} into a Calico selector string."""
    if not labels:
        return "all()"

    parts = []
    for key, value in sorted(labels.items()):
        safe_key = str(key).replace("'", "\\'")
        safe_value = str(value).replace("'", "\\'")
        parts.append(f"{safe_key} == '{safe_value}'")

    return " && ".join(parts)


def _namespace_selector(namespace: str) -> str:
    """Calico can match a namespace by using the projectcalico.org/name label."""
    safe_namespace = str(namespace).replace("'", "\\'")
    return f"projectcalico.org/name == '{safe_namespace}'"


def _json_response(handler, status: int, payload: dict):
    """Write a JSON response."""
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler, status: int, content: str):
    """Write a text response."""
    body = content.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler) -> dict:
    """Read and parse the incoming JSON body."""
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length == 0:
        raise ValueError("request body is empty")

    raw_body = handler.rfile.read(content_length)
    try:
        return json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


def _validate_policy_request(payload: dict):
    """Validate the request fields for creating the bidirectional block."""
    required = ["name", "sourceNamespace", "sourceLabels", "targetNamespace", "targetLabels"]
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    if not isinstance(payload["sourceLabels"], dict):
        raise ValueError("sourceLabels must be an object")
    if not isinstance(payload["targetLabels"], dict):
        raise ValueError("targetLabels must be an object")

    for field in ["name", "sourceNamespace", "targetNamespace"]:
        if not str(payload[field]).strip():
            raise ValueError(f"{field} cannot be empty")


def _build_calico_policy_object(policy_name: str, namespace: str, selector: str,
                                peer_namespace: str, peer_selector: str, policy_group: str) -> dict:
    """Create one Calico namespaced NetworkPolicy object."""
    return {
        "apiVersion": f"{CALICO_GROUP}/{CALICO_VERSION}",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": policy_name,
            "namespace": namespace,
            "labels": {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                POLICY_GROUP_LABEL: policy_group,
            },
        },
        "spec": {
            "selector": selector,
            "types": ["Ingress", "Egress"],
            "ingress": [
                {
                    "action": "Deny",
                    "source": {
                        "namespaceSelector": _namespace_selector(peer_namespace),
                        "selector": peer_selector,
                    },
                }
            ],
            "egress": [
                {
                    "action": "Deny",
                    "destination": {
                        "namespaceSelector": _namespace_selector(peer_namespace),
                        "selector": peer_selector,
                    },
                }
            ],
        },
    }


def _build_bidirectional_policy_objects(payload: dict) -> list:
    """Create the two Calico policies needed for a full block in both directions."""
    base_name = payload["name"].strip()
    source_namespace = payload["sourceNamespace"].strip()
    target_namespace = payload["targetNamespace"].strip()

    source_selector = _build_selector(payload["sourceLabels"])
    target_selector = _build_selector(payload["targetLabels"])

    source_policy_name = f"{base_name}-source"
    target_policy_name = f"{base_name}-target"

    source_policy = _build_calico_policy_object(
        policy_name=source_policy_name,
        namespace=source_namespace,
        selector=source_selector,
        peer_namespace=target_namespace,
        peer_selector=target_selector,
        policy_group=base_name,
    )

    target_policy = _build_calico_policy_object(
        policy_name=target_policy_name,
        namespace=target_namespace,
        selector=target_selector,
        peer_namespace=source_namespace,
        peer_selector=source_selector,
        policy_group=base_name,
    )

    return [source_policy, target_policy]


def list_deployment_health() -> dict:
    """Return deployment replica health across all namespaces."""
    apps_api = STATE["apps_api"]
    if apps_api is None:
        raise RuntimeError("Kubernetes client is not configured")

    deployments = apps_api.list_deployment_for_all_namespaces().items
    items = []

    for dep in deployments:
        requested = dep.spec.replicas if dep.spec and dep.spec.replicas is not None else 1
        ready = dep.status.ready_replicas if dep.status and dep.status.ready_replicas is not None else 0
        available = dep.status.available_replicas if dep.status and dep.status.available_replicas is not None else 0

        items.append({
            "namespace": dep.metadata.namespace,
            "name": dep.metadata.name,
            "requestedReplicas": requested,
            "readyReplicas": ready,
            "availableReplicas": available,
            "healthy": ready == requested,
        })

    unhealthy = [item for item in items if not item["healthy"]]

    return {
        "totalDeployments": len(items),
        "healthyDeployments": len(items) - len(unhealthy),
        "unhealthyDeployments": len(unhealthy),
        "items": items,
    }


def list_network_policies() -> dict:
    """List Calico NetworkPolicies from all namespaces."""
    custom_api = STATE["custom_api"]
    if custom_api is None:
        raise RuntimeError("Kubernetes client is not configured")

    result = custom_api.list_cluster_custom_object(
        group=CALICO_GROUP,
        version=CALICO_VERSION,
        plural=CALICO_PLURAL,
    )

    items = []
    grouped = {}

    for policy in result.get("items", []):
        metadata = policy.get("metadata", {})
        labels = metadata.get("labels", {})
        spec = policy.get("spec", {})

        item = {
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace"),
            "selector": spec.get("selector"),
            "types": spec.get("types", []),
            "managedByTool": labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE,
            "policyGroup": labels.get(POLICY_GROUP_LABEL),
        }
        items.append(item)

        policy_group = labels.get(POLICY_GROUP_LABEL)
        if policy_group:
            grouped.setdefault(policy_group, []).append({
                "name": metadata.get("name"),
                "namespace": metadata.get("namespace"),
            })

    return {
        "count": len(items),
        "items": items,
        "managedGroups": grouped,
    }


def create_bidirectional_policy(payload: dict) -> dict:
    """Create two Calico policies, one in each namespace, to block both directions."""
    _validate_policy_request(payload)

    custom_api = STATE["custom_api"]
    if custom_api is None:
        raise RuntimeError("Kubernetes client is not configured")

    objects = _build_bidirectional_policy_objects(payload)
    created = []

    try:
        for obj in objects:
            namespace = obj["metadata"]["namespace"]
            created_obj = custom_api.create_namespaced_custom_object(
                group=CALICO_GROUP,
                version=CALICO_VERSION,
                namespace=namespace,
                plural=CALICO_PLURAL,
                body=obj,
            )
            created.append({
                "name": created_obj["metadata"]["name"],
                "namespace": created_obj["metadata"]["namespace"],
            })
    except Exception:
        # Best effort cleanup if only one of the two objects was created.
        for item in created:
            try:
                custom_api.delete_namespaced_custom_object(
                    group=CALICO_GROUP,
                    version=CALICO_VERSION,
                    namespace=item["namespace"],
                    plural=CALICO_PLURAL,
                    name=item["name"],
                )
            except Exception:
                pass
        raise

    return {
        "message": "bidirectional network block created",
        "policyGroup": payload["name"],
        "createdPolicies": created,
    }


def delete_bidirectional_policy(policy_group: str) -> dict:
    """Delete both policies that belong to the given policy group name."""
    custom_api = STATE["custom_api"]
    if custom_api is None:
        raise RuntimeError("Kubernetes client is not configured")

    result = custom_api.list_cluster_custom_object(
        group=CALICO_GROUP,
        version=CALICO_VERSION,
        plural=CALICO_PLURAL,
    )

    matches = []
    for item in result.get("items", []):
        labels = item.get("metadata", {}).get("labels", {})
        if labels.get(POLICY_GROUP_LABEL) == policy_group:
            matches.append({
                "name": item["metadata"]["name"],
                "namespace": item["metadata"]["namespace"],
            })

    if not matches:
        raise ApiException(status=404, reason=f"no policies found for policy group '{policy_group}'")

    deleted = []
    for item in matches:
        custom_api.delete_namespaced_custom_object(
            group=CALICO_GROUP,
            version=CALICO_VERSION,
            namespace=item["namespace"],
            plural=CALICO_PLURAL,
            name=item["name"],
        )
        deleted.append(item)

    return {
        "message": "bidirectional network block deleted",
        "policyGroup": policy_group,
        "deletedPolicies": deleted,
    }


def kubernetes_ready() -> dict:
    """Check if the tool can talk to the configured Kubernetes API server."""
    api_client = STATE["api_client"]
    if api_client is None:
        raise RuntimeError("Kubernetes client is not configured")

    version = get_kubernetes_version(api_client)
    return {
        "ready": True,
        "kubernetesVersion": version,
    }


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/healthz":
                self.healthz()
            elif path == "/readyz":
                self.readyz()
            elif path == "/deployments/health":
                self.get_deployments_health()
            elif path == "/networkpolicies":
                self.get_network_policies()
            else:
                self.send_error(404)
        except ApiException as exc:
            status = exc.status if exc.status else 500
            _json_response(self, status, {"error": str(exc)})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)})

    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/networkpolicies":
                self.create_network_policy()
            else:
                self.send_error(404)
        except ApiException as exc:
            status = exc.status if exc.status else 500
            _json_response(self, status, {"error": str(exc)})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)})

    def do_DELETE(self):
        """Handle DELETE requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path.startswith("/networkpolicies/"):
                self.delete_network_policy(path)
            else:
                self.send_error(404)
        except ApiException as exc:
            status = exc.status if exc.status else 500
            _json_response(self, status, {"error": str(exc)})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)})

    def log_message(self, format, *args):
        """Keep logs simple and useful."""
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format % args))

    def healthz(self):
        """Liveness endpoint used by tests and Kubernetes probes."""
        _text_response(self, 200, "ok")

    def readyz(self):
        """Readiness endpoint that checks Kubernetes API access."""
        try:
            result = kubernetes_ready()
            _json_response(self, 200, result)
        except Exception as exc:
            _json_response(self, 503, {"ready": False, "error": str(exc)})

    def get_deployments_health(self):
        """Return requested vs ready pod counts for deployments."""
        result = list_deployment_health()
        _json_response(self, 200, result)

    def get_network_policies(self):
        """Return Calico policies and grouped tool-managed policy pairs."""
        result = list_network_policies()
        _json_response(self, 200, result)

    def create_network_policy(self):
        """Create the bidirectional Calico policy pair."""
        payload = _read_json_body(self)
        result = create_bidirectional_policy(payload)
        _json_response(self, 201, result)

    def delete_network_policy(self, path: str):
        """Delete the policy pair by group name."""
        policy_group = path.rsplit("/", 1)[-1].strip()
        if not policy_group:
            raise ValueError("policy name is required")

        result = delete_bidirectional_policy(policy_group)
        _json_response(self, 200, result)


def start_server(address: str):
    """
    Start the HTTP server and block until it stops.

    Expected address format: host:port
    Example: :8080 or 0.0.0.0:8080
    """
    global SERVER

    try:
        host, port = address.split(":")
    except ValueError:
        raise ValueError("invalid server address format, expected host:port")

    if host == "":
        host = "0.0.0.0"

    SERVER = ThreadingHTTPServer((host, int(port)), AppHandler)
    print(f"Server listening on {host}:{port}")

    try:
        SERVER.serve_forever()
    finally:
        SERVER.server_close()
        SERVER = None


def stop_server():
    """Gracefully stop the HTTP server."""
    global SERVER
    if SERVER is not None:
        SERVER.shutdown()

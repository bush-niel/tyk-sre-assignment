import json
import socketserver
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from kubernetes import client


API_CLIENT = None
SHUTTING_DOWN = False
MANAGED_BY_LABEL_KEY = "managed-by"
MANAGED_BY_LABEL_VALUE = "tyk-sre-tool"
CALICO_GROUP = "crd.projectcalico.org"
CALICO_VERSION = "v1"
CALICO_PLURAL = "networkpolicies"
CALICO_KIND = "NetworkPolicy"
CALICO_API_VERSION = "crd.projectcalico.org/v1"


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Route GET requests to the set of endpoints this tool supports."""
        parsed = urlparse(self.path)

        if parsed.path == "/healthz":
            self.healthz()
        elif parsed.path == "/readyz":
            self.readyz()
        elif parsed.path == "/deployments/health":
            self.deployments_health()
        elif parsed.path == "/networkpolicies":
            self.list_networkpolicies(parsed.query)
        else:
            self.send_error(404)

    def do_POST(self):
        """Handle policy creation requests."""
        parsed = urlparse(self.path)

        if parsed.path == "/networkpolicies":
            self.create_networkpolicy()
        else:
            self.send_error(404)

    def do_DELETE(self):
        """Delete one managed network policy by name."""
        parsed = urlparse(self.path)

        if parsed.path.startswith("/networkpolicies/"):
            policy_name = parsed.path.split("/")[-1]
            self.delete_networkpolicy(policy_name)
        else:
            self.send_error(404)

    def log_message(self, _format, *_args):
        return

    def healthz(self):
        """Simple process health to let old tests pass."""
        self.respond(200, "ok")

    def readyz(self):
        """Readiness means the process is up and it can still talk to the Kubernetes API."""
        if get_shutting_down():
            self.respond_json(503, {"ready": False, "reason": "server is shutting down"})
            return

        try:
            get_kubernetes_version(get_api_client())
            self.respond_json(200, {"ready": True})
        except Exception as exc:
            self.respond_json(503, {"ready": False, "error": str(exc)})

    def deployments_health(self):
        """Show whether each Deployment has the ready pods it asked for."""
        try:
            data = get_deployments_health(get_api_client())
            self.respond_json(200, data)
        except Exception as exc:
            self.respond_json(500, {"error": str(exc)})

    def list_networkpolicies(self, query_string: str):
        """List Calico global network policies. Optionally show only the ones this tool created."""
        try:
            query = parse_qs(query_string)
            managed_only = query.get("managed_only", ["false"])[0].lower() == "true"
            data = list_calico_networkpolicies(get_api_client(), managed_only)
            self.respond_json(200, data)
        except Exception as exc:
            self.respond_json(500, {"error": str(exc)})

    def create_networkpolicy(self):
        """Create the two deny rules needed to block traffic both ways."""
        try:
            payload = self.read_json_body()
            validate_policy_request(payload)
            result = create_bidirectional_deny_policy(get_api_client(), payload)
            self.respond_json(201, result)
        except ValueError as exc:
            self.respond_json(400, {"error": str(exc)})
        except Exception as exc:
            self.respond_json(500, {"error": str(exc)})

    def delete_networkpolicy(self, policy_name: str):
        """Delete a managed policy by name. This avoids removing policies owned by someone else."""
        try:
            result = delete_calico_networkpolicy(get_api_client(), policy_name)
            self.respond_json(200, result)
        except ValueError as exc:
            self.respond_json(400, {"error": str(exc)})
        except Exception as exc:
            self.respond_json(500, {"error": str(exc)})

    def read_json_body(self):
        """Read the request body and turn it into JSON."""
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}")

    def respond(self, status: int, content: str):
        """Write a plain text response."""
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(bytes(content, "utf-8"))

    def respond_json(self, status: int, content):
        """Write a JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(content, indent=2).encode("utf-8"))


def set_api_client(api_client: client.ApiClient):
    """Save one shared Kubernetes client for all request handlers."""
    global API_CLIENT
    API_CLIENT = api_client


def get_api_client() -> client.ApiClient:
    """Return the shared API client, or a new one"""
    if API_CLIENT is None:
        return client.ApiClient()
    return API_CLIENT


def set_shutting_down(value: bool):
    """Readiness should fail once shutdown has started."""
    global SHUTTING_DOWN
    SHUTTING_DOWN = value


def get_shutting_down() -> bool:
    """Tell handlers whether the process is shutting down."""
    return SHUTTING_DOWN


def get_kubernetes_version(api_client: client.ApiClient) -> str:
    """Ask the Kubernetes API server for its git version string."""
    version = client.VersionApi(api_client).get_code()
    return version.git_version


def get_deployments_health(api_client: client.ApiClient):
    """Compare requested replicas with ready replicas for every Deployment."""
    apps_api = client.AppsV1Api(api_client)
    deployments = apps_api.list_deployment_for_all_namespaces().items

    results = []
    all_healthy = True

    for item in deployments:
        requested = item.spec.replicas or 0
        healthy = item.status.ready_replicas or 0
        is_healthy = healthy == requested
        all_healthy = all_healthy and is_healthy

        results.append(
            {
                "namespace": item.metadata.namespace,
                "name": item.metadata.name,
                "requestedReplicas": requested,
                "healthyReplicas": healthy,
                "healthy": is_healthy,
            }
        )

    return {"healthy": all_healthy, "deployments": results}


def list_calico_networkpolicies(api_client: client.ApiClient, managed_only: bool = False):
    """List Calico network policies and tell the caller which ones belong to this tool."""
    custom_api = client.CustomObjectsApi(api_client)
    response = custom_api.list_cluster_custom_object(
        group=CALICO_GROUP,
        version=CALICO_VERSION,
        plural=CALICO_PLURAL,
    )

    items = []
    for item in response.get("items", []):
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        managed = labels.get(MANAGED_BY_LABEL_KEY) == MANAGED_BY_LABEL_VALUE

        if managed_only and not managed:
            continue

        spec = item.get("spec", {})
        items.append(
            {
                "name": metadata.get("name"),
                "managed": managed,
                "selector": spec.get("selector", ""),
                "types": spec.get("types", []),
                "order": spec.get("order"),
                "ingress": spec.get("ingress", []),
                "egress": spec.get("egress", []),
            }
        )

    return {"items": items}


def validate_policy_request(payload: dict):
    """Validate the simple request format used by this tool.

    Expected format:
    {
      "name": "team-a-to-team-b",
      "sourceNamespace": "team-a",
      "sourceLabels": {"app": "api-a"},
      "targetNamespace": "team-b",
      "targetLabels": {"app": "api-b"}
    }
    """
    required_fields = [
        "name",
        "sourceNamespace",
        "sourceLabels",
        "targetNamespace",
        "targetLabels",
    ]

    for field in required_fields:
        if field not in payload:
            raise ValueError(f"missing required field: {field}")

    if not isinstance(payload["sourceLabels"], dict) or not payload["sourceLabels"]:
        raise ValueError("sourceLabels must be a non-empty JSON object")

    if not isinstance(payload["targetLabels"], dict) or not payload["targetLabels"]:
        raise ValueError("targetLabels must be a non-empty JSON object")


def labels_to_calico_selector(labels: dict) -> str:
    """Turn a simple label map into a Calico selector string."""
    parts = []

    for key in sorted(labels.keys()):
        value = str(labels[key]).replace("'", "\\'")
        parts.append(f"{key} == '{value}'")

    return " && ".join(parts)


def build_policy_name(base_name: str, direction: str) -> str:
    """Give the two policies predictable names so they are easy to list and delete."""
    return f"{base_name}-{direction}"


def build_policy_labels(base_name: str) -> dict:
    """Attach ownership labels so the tool can safely find only its own policies."""
    return {
        MANAGED_BY_LABEL_KEY: MANAGED_BY_LABEL_VALUE,
        "block-group": base_name,
    }


def build_deny_policy_document(name: str, base_name: str, source_namespace: str, source_labels: dict,
                               other_namespace: str, other_labels: dict, direction: str) -> dict:
    """Build one Calico GlobalNetworkPolicy that blocks one side of the traffic."""
    source_selector = labels_to_calico_selector(source_labels)
    other_selector = labels_to_calico_selector(other_labels)

    if direction == "ingress":
        ingress_rules = [
            {
                "action": "Deny",
                "source": {
                    "namespaceSelector": f"projectcalico.org/name == '{other_namespace}'",
                    "selector": other_selector,
                },
            }
        ]
        egress_rules = []
    else:
        ingress_rules = []
        egress_rules = [
            {
                "action": "Deny",
                "destination": {
                    "namespaceSelector": f"projectcalico.org/name == '{other_namespace}'",
                    "selector": other_selector,
                },
            }
        ]

    return {
        "apiVersion": CALICO_API_VERSION,
        "kind": CALICO_KIND,
        "metadata": {
            "name": name,
            "labels": build_policy_labels(base_name),
        },
        "spec": {
            "order": 1000,
            "namespaceSelector": f"projectcalico.org/name == '{source_namespace}'",
            "selector": source_selector,
            "types": ["Ingress", "Egress"],
            "ingress": ingress_rules,
            "egress": egress_rules,
        },
    }


def create_bidirectional_deny_policy(api_client: client.ApiClient, payload: dict):
    """Create two policies so traffic is blocked in both directions."""
    custom_api = client.CustomObjectsApi(api_client)

    base_name = payload["name"]
    source_namespace = payload["sourceNamespace"]
    source_labels = payload["sourceLabels"]
    target_namespace = payload["targetNamespace"]
    target_labels = payload["targetLabels"]

    policies = [
        build_deny_policy_document(
            build_policy_name(base_name, "source-egress"),
            base_name,
            source_namespace,
            source_labels,
            target_namespace,
            target_labels,
            "egress",
        ),
        build_deny_policy_document(
            build_policy_name(base_name, "target-egress"),
            base_name,
            target_namespace,
            target_labels,
            source_namespace,
            source_labels,
            "egress",
        ),
    ]

    created_names = []
    for policy in policies:
        custom_api.create_cluster_custom_object(
            group=CALICO_GROUP,
            version=CALICO_VERSION,
            plural=CALICO_PLURAL,
            body=policy,
        )
        created_names.append(policy["metadata"]["name"])

    return {
        "message": "bidirectional deny policies created",
        "policies": created_names,
    }


def delete_calico_networkpolicy(api_client: client.ApiClient, policy_name: str):
    """Delete a policy only if this tool created it."""
    custom_api = client.CustomObjectsApi(api_client)

    existing = custom_api.get_cluster_custom_object(
        group=CALICO_GROUP,
        version=CALICO_VERSION,
        plural=CALICO_PLURAL,
        name=policy_name,
    )

    labels = existing.get("metadata", {}).get("labels", {})
    managed = labels.get(MANAGED_BY_LABEL_KEY) == MANAGED_BY_LABEL_VALUE

    if not managed:
        raise ValueError("refusing to delete unmanaged policy")

    custom_api.delete_cluster_custom_object(
        group=CALICO_GROUP,
        version=CALICO_VERSION,
        plural=CALICO_PLURAL,
        name=policy_name,
    )

    return {"message": "network policy deleted", "name": policy_name}


def create_server(address: str):
    """Create the HTTP server. We keep this separate so that main.py can manage shutdown."""
    try:
        host, port = address.split(":", 1)
    except ValueError:
        raise ValueError("invalid server address format, expected host:port")

    return ThreadingHTTPServer((host, int(port)), AppHandler)


def start_server(address: str):
    """Compatibility wrapper for the original code path."""
    with create_server(address) as httpd:
        print(f"Server listening on {address}")
        httpd.serve_forever()

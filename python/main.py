import sys
import argparse


from kubernetes import client, config

from app import app



def load_kubernetes_config(kubeconfig_path: str):
    """Load kubeconfig locally or in-cluster config in Kubernetes."""
    if kubeconfig_path:
        config.load_kube_config(config_file=kubeconfig_path)
        return "kubeconfig"

    config.load_incluster_config()
    return "in-cluster"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tyk SRE Assignment",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-k",
        "--kubeconfig",
        type=str,
        default="",
        help="path to kubeconfig, leave empty for in-cluster",
    )
    parser.add_argument(
        "-a",
        "--address",
        type=str,
        default="0.0.0.0:8080",
        help="HTTP server listen address",
    )
    args = parser.parse_args()

    try:
        mode = load_kubernetes_config(args.kubeconfig)
        api_client = client.ApiClient()
        app.configure_kubernetes(api_client)
        version = app.get_kubernetes_version(api_client)
    except Exception as exc:
        print(f"failed to connect to Kubernetes: {exc}")
        sys.exit(1)

    print(f"Connected to Kubernetes {version} using {mode} config")

    try:
        app.start_server(args.address)
    except KeyboardInterrupt:
        print("Server terminated")


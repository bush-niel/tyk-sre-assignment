import argparse
import signal
import sys

from kubernetes import client, config

from app import app


HTTP_SERVER = None
SHUTTING_DOWN = False


def handle_shutdown(signum, _frame):
    """Stop the server cleanly when the process gets a stop signal."""
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    print(f"Received signal {signum}. Shutting down server.")
    app.set_shutting_down(True)

    if HTTP_SERVER is not None:
        HTTP_SERVER.shutdown()


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
        default=":8080",
        help="HTTP server listen address",
    )
    args = parser.parse_args()

    if args.kubeconfig:
        config.load_kube_config(config_file=args.kubeconfig)
    else:
        config.load_incluster_config()

    api_client = client.ApiClient()
    app.set_api_client(api_client)

    try:
        version = app.get_kubernetes_version(api_client)
    except Exception as exc:
        print(f"Failed to connect to Kubernetes API: {exc}")
        sys.exit(1)

    print(f"Connected to Kubernetes {version}")

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        HTTP_SERVER = app.create_server(args.address)
        print(f"Server listening on {args.address}")
        HTTP_SERVER.serve_forever()
    except KeyboardInterrupt:
        print("Server terminated")
    finally:
        if HTTP_SERVER is not None:
            HTTP_SERVER.server_close()

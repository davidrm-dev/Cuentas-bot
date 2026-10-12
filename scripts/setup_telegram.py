"""Configura el bot en Telegram después de `sam deploy`.

Uso:
    python scripts/setup_telegram.py --stack cuentas-bot [--region us-east-1]

Lee las salidas del stack y los parámetros SSM, y luego:
* registra el webhook (con secret_token, solo updates de mensajes y botones),
* publica la lista de comandos,
* fija el botón de menú por defecto que abre la Mini App.
"""
from __future__ import annotations

import argparse
import json
import urllib.request

import boto3


def call(token: str, method: str, **params: object) -> object:
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    if not data.get("ok"):
        raise SystemExit(f"{method} falló: {data}")
    return data["result"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default="cuentas-bot")
    parser.add_argument("--region", default=None)
    parser.add_argument("--token-param", default="/cuentas-bot/telegram-token")
    parser.add_argument("--secret-param", default="/cuentas-bot/webhook-secret")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    stack = session.client("cloudformation").describe_stacks(StackName=args.stack)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    ssm = session.client("ssm")
    token = ssm.get_parameter(Name=args.token_param, WithDecryption=True)["Parameter"]["Value"]
    secret = ssm.get_parameter(Name=args.secret_param, WithDecryption=True)["Parameter"]["Value"]

    call(token, "setWebhook", url=outputs["WebhookUrl"], secret_token=secret,
         allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    call(token, "setMyCommands", commands=[
        {"command": "resumen", "description": "Resumen del mes"},
        {"command": "dashboard", "description": "Abrir el dashboard completo"},
        {"command": "prestamos", "description": "Lo que te deben y lo que debes"},
        {"command": "fijos", "description": "Suscripciones y gastos fijos apartados"},
        {"command": "deshacer", "description": "Borrar el último movimiento"},
        {"command": "consumo", "description": "Cuánto cuesta el bot este mes"},
        {"command": "ayuda", "description": "Qué puedo hacer"},
    ])
    call(token, "setChatMenuButton",
         menu_button={"type": "web_app", "text": "Dashboard", "web_app": {"url": outputs["WebAppUrl"]}})
    print("Webhook:", outputs["WebhookUrl"])
    print("Mini App:", outputs["WebAppUrl"])
    print("Listo ✅")


if __name__ == "__main__":
    main()

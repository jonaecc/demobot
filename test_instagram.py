#!/usr/bin/env python3
"""
test_instagram.py — Simulador local de publicaciones en Instagram
====================================================================
Prueba la integración de Instagram SIN correr el bot de trading.
Solo importa instagram_poster.py — nunca live_worker.py, ccxt ni binance,
así que se puede correr en cualquier máquina, sin API keys de Binance.

Las credenciales de Instagram/imgbb se leen de un archivo ".env" en la
raíz del proyecto (mismo directorio que este script). Ver .env.example.
En Railway no hace falta nada extra: ahí ya están seteadas como variables
de entorno del servicio, y este mismo código las toma igual (el .env
nunca pisa una variable que ya exista en el entorno real).

USO
────────────────────────────────────────────────────────────────────
  Vista previa rápida, sin publicar nada ni necesitar credenciales:
    python test_instagram.py apertura --dry-run
    python test_instagram.py cierre --dry-run

  Simulación completa (genera la imagen, la sube y la publica de verdad
  en tu cuenta de Instagram — requiere el .env completo):
    python test_instagram.py apertura
    python test_instagram.py cierre

  Con datos propios en vez de los de ejemplo:
    python test_instagram.py apertura --symbol BTC/USDT --side sell \
        --entry 67210 --tp 2.8 --sl 0.9 --leverage 15 --scenario T1

    python test_instagram.py cierre --symbol BTC/USDT --side sell \
        --entry 67210 --exit 66430 --pnl 112.80

  Si tu IG_USER_ID y tu IG_ACCESS_TOKEN no combinan (error "Object does not
  exist" o "Cannot parse access token"), este comando busca, usando tu
  token real, cuál es el IG_USER_ID que realmente le corresponde:
    python test_instagram.py diagnosticar
"""

import argparse
import os

from instagram_poster import (
    generar_imagen_apertura, generar_imagen_cierre,
    post_apertura, post_cierre, diagnosticar_configuracion, diagnosticar_token,
    listar_cuentas_disponibles,
)


class ConsoleLogger:
    """Reemplaza el logger del bot real — solo imprime en la consola."""
    def info(self, msg):     print(f"  · {msg}")
    def success(self, msg):  print(f"  ✅ {msg}")
    def warning(self, msg):  print(f"  ⚠️  {msg}")
    def error(self, msg):    print(f"  ❌ {msg}")
    def critical(self, msg): print(f"  🛑 {msg}")


def _chequear_config():
    estado = diagnosticar_configuracion()
    print("Configuración detectada (.env / variables de entorno):")
    for clave, ok in estado.items():
        print(f"  [{'OK' if ok else 'FALTA'.ljust(2)}] {clave}")

    if estado.get("IG_ACCESS_TOKEN"):
        info = diagnosticar_token()
        print(f"\n  Diagnóstico de IG_ACCESS_TOKEN:")
        print(f"    · Largo: {info['longitud']} caracteres "
              f"(los tokens de Instagram suelen rondar 190-230)")
        print(f"    · Vista previa: {info['preview']}")
        if info["tenia_espacios_o_comillas_de_mas"]:
            print("    ⚠️  Tenía espacios, saltos de línea o comillas de más — "
                  "ya se limpiaron solos, pero revisá que el .env tenga el "
                  "token completo en UNA sola línea, sin comillas.")
    print()
    return all(estado.values())


def _guardar_preview(img_bytes, nombre):
    os.makedirs("test_output", exist_ok=True)
    path = os.path.join("test_output", nombre)
    with open(path, "wb") as f:
        f.write(img_bytes)
    print(f"  🖼  Imagen guardada en {path}  (modo --dry-run: no se publicó nada)")


def cmd_apertura(args):
    print(f"\n▶ Simulando APERTURA — {args.symbol} {args.side.upper()}\n")

    if args.dry_run:
        img = generar_imagen_apertura(
            symbol=args.symbol, side=args.side, entry_price=args.entry,
            tp_pct=args.tp, sl_pct=args.sl, leverage=args.leverage,
            scenario=args.scenario, fondo_path=os.environ.get("IG_BACKGROUND_PATH"),
        )
        _guardar_preview(img, "apertura_preview.png")
        return

    if not _chequear_config():
        print("Faltan credenciales — no se puede publicar de verdad.\n"
              "Completá tu .env (ver .env.example) o agregá --dry-run para\n"
              "solo generar la imagen sin publicar.\n")
        return

    post_apertura(
        side=args.side, symbol=args.symbol, entry_price=args.entry,
        tp_pct=args.tp, sl_pct=args.sl, leverage=args.leverage,
        scenario=args.scenario, log=ConsoleLogger(),
    )


def cmd_cierre(args):
    print(f"\n▶ Simulando CIERRE — {args.symbol} {args.side.upper()}\n")

    if args.dry_run:
        img = generar_imagen_cierre(
            symbol=args.symbol, side=args.side, entry_price=args.entry,
            exit_price=args.exit, pnl_usd=args.pnl,
            scenario=args.scenario, fondo_path=os.environ.get("IG_BACKGROUND_PATH"),
        )
        _guardar_preview(img, "cierre_preview.png")
        return

    if not _chequear_config():
        print("Faltan credenciales — no se puede publicar de verdad.\n"
              "Completá tu .env (ver .env.example) o agregá --dry-run para\n"
              "solo generar la imagen sin publicar.\n")
        return

    post_cierre(
        side=args.side, symbol=args.symbol, entry_price=args.entry,
        exit_price=args.exit, pnl_usd=args.pnl,
        scenario=args.scenario, log=ConsoleLogger(),
    )


def cmd_diagnosticar(args):
    print("\n▶ Buscando el IG_USER_ID correcto para tu IG_ACCESS_TOKEN...\n")
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not token:
        print("No hay IG_ACCESS_TOKEN en tu .env todavía.")
        return

    if not token.startswith("EAA"):
        print("Tu token no empieza con 'EAA', así que probablemente no es de\n"
              "Facebook Login for Business — este diagnóstico es para ese caso.\n"
              "Si lo generaste desde 'API setup with Instagram login', ese panel\n"
              "ya te muestra el ID correcto directamente, junto al token.\n")
        return

    try:
        cuentas = listar_cuentas_disponibles(token)
    except Exception as e:
        print(f"  ❌ {e}\n")
        return

    if not cuentas:
        print("Tu token no tiene acceso a ninguna Página de Facebook.\n"
              "Revisá que al generarlo hayas seleccionado la Página correcta\n"
              "y los permisos pages_show_list, instagram_business_basic e\n"
              "instagram_business_content_publish.\n")
        return

    print("Páginas de Facebook accesibles con este token:\n")
    for c in cuentas:
        if c["ig_user_id"]:
            print(f"  · Página \"{c['nombre']}\" → IG_USER_ID correcto: {c['ig_user_id']}")
        else:
            print(f"  · Página \"{c['nombre']}\" (id {c['page_id']}) → "
                  f"sin cuenta de Instagram profesional vinculada")
    print("\nCopiá el IG_USER_ID de la página correcta a tu .env y volvé a probar.\n")


def main():
    parser = argparse.ArgumentParser(
        description="Simula una operación del bot para probar la publicación en Instagram, sin correr el bot real."
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p_ap = sub.add_parser("apertura", help="Simula la apertura de una operación")
    p_ap.add_argument("--symbol", default="ETH/USDT")
    p_ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    p_ap.add_argument("--entry", type=float, default=3421.55)
    p_ap.add_argument("--tp", type=float, default=3.5)
    p_ap.add_argument("--sl", type=float, default=1.0)
    p_ap.add_argument("--leverage", type=int, default=10)
    p_ap.add_argument("--scenario", default="T2__UU_DD_UD")
    p_ap.add_argument("--dry-run", action="store_true",
                       help="Solo genera y guarda la imagen; no sube ni publica nada")
    p_ap.set_defaults(func=cmd_apertura)

    p_ci = sub.add_parser("cierre", help="Simula el cierre de una operación")
    p_ci.add_argument("--symbol", default="ETH/USDT")
    p_ci.add_argument("--side", choices=["buy", "sell"], default="buy")
    p_ci.add_argument("--entry", type=float, default=3421.55)
    p_ci.add_argument("--exit", type=float, default=3540.10)
    p_ci.add_argument("--pnl", type=float, default=64.20)
    p_ci.add_argument("--scenario", default="T2__UU_DD_UD")
    p_ci.add_argument("--dry-run", action="store_true",
                       help="Solo genera y guarda la imagen; no sube ni publica nada")
    p_ci.set_defaults(func=cmd_cierre)

    p_di = sub.add_parser("diagnosticar",
                           help="Busca el IG_USER_ID correcto para tu IG_ACCESS_TOKEN")
    p_di.set_defaults(func=cmd_diagnosticar)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

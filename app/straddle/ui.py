"""Streamlit presentation for the StraddleLab paper-trading workflow."""

from datetime import date
from decimal import Decimal

import httpx
import streamlit as st


def _call(client, method: str, path: str, **kwargs):
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        st.error(f"Backend unavailable: {exc}")
        return None
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.json())
            if isinstance(detail, dict):
                message = detail.get("message", detail.get("code", str(detail)))
            else:
                message = str(detail)
        except ValueError:
            message = "The request could not be completed."
        st.error(message)
        return None
    return response


def _money(value):
    return "—" if value is None else f"₹{value}"


def _rule_groups(results):
    groups = {"BLOCKING": [], "WARNING": [], "INFO": []}
    for result in results:
        if result.get("blocking"):
            groups["BLOCKING"].append(result)
        else:
            groups.get(result.get("severity"), groups["INFO"]).append(result)
    return groups


def _show_rules(results):
    groups = _rule_groups(results)
    for label in ("BLOCKING", "WARNING", "INFO"):
        items = groups[label]
        if not items:
            continue
        st.markdown(f"**{label}**")
        for item in items:
            acknowledgement = (
                " — acknowledgement required"
                if item.get("requires_acknowledgement")
                else ""
            )
            st.write(f"{item['rule_id']}: {item['message']}{acknowledgement}")


def _construction(client):
    st.header("1. CONSTRUCT")
    underlyings_response = _call(client, "GET", "/straddle/underlyings")
    if underlyings_response is None:
        st.info("Dhan options market data is currently unavailable.")
        return
    underlyings = underlyings_response.json().get("underlyings", [])
    if not underlyings:
        st.info("No supported option underlyings are available.")
        return
    underlying = st.selectbox("Underlying", underlyings, key="sl_underlying")
    expiry_response = _call(
        client,
        "GET",
        f"/straddle/expiries/{underlying}",
        params={"evaluation_date": date.today().isoformat()},
    )
    if expiry_response is None:
        return
    expiries = expiry_response.json().get("expiries", [])
    if not expiries:
        st.warning("No unexpired option contracts are available.")
        return
    expiry = st.selectbox("Expiry", expiries, key="sl_expiry")
    chain_key = f"{underlying}:{expiry}"
    if st.button("Load option chain"):
        chain_response = _call(
            client, "GET", f"/straddle/chain/{underlying}/{expiry}"
        )
        if chain_response is not None:
            st.session_state["sl_chain"] = chain_response.json()
            st.session_state["sl_chain_key"] = chain_key
    chain = (
        st.session_state.get("sl_chain")
        if st.session_state.get("sl_chain_key") == chain_key
        else None
    )
    if chain is None:
        st.caption("Load the selected expiry's option chain to continue.")
        return
    strikes = sorted(
        {item["strike"] for item in chain.get("contracts", [])}, key=Decimal
    )
    strike_mode = st.radio(
        "Strike selection",
        ("Recommended ATM", "Manual override"),
        horizontal=True,
    )
    selected_strike = (
        st.selectbox("Available strike", strikes)
        if strike_mode == "Manual override" and strikes
        else None
    )
    st.write(f"Current spot: {_money(chain.get('spot_price'))}")
    st.write(f"Provider timestamp: {chain.get('provider_timestamp') or 'Unavailable'}")
    st.write(f"Received at: {chain.get('received_at')}")
    st.caption("Freshness is evaluated by the deterministic rules engine.")

    if st.button("Construct long straddle", type="primary"):
        payload = {
            "underlying": underlying,
            "expiry": expiry,
            "selected_strike": selected_strike,
        }
        response = _call(
            client, "POST", "/straddle/strategies/construct", json=payload
        )
        if response is not None:
            st.session_state["sl_construction"] = response.json()
            st.session_state.pop("sl_approval", None)
            st.session_state.pop("sl_position_id", None)


def _validation_calculation(client):
    outcome = st.session_state.get("sl_construction")
    if not outcome:
        return
    construction = outcome["construction"]
    snapshot = outcome["snapshot"]
    selected = construction.get("selected_strike")
    matching = [
        item for item in snapshot.get("contracts", []) if item["strike"] == selected
    ]
    st.write(f"Recommended ATM: {construction.get('recommended_atm_strike')}")
    st.write(f"Selected strike: {selected}")
    for contract in matching:
        st.write(
            f"{contract['option_type']}: premium {_money(contract['premium'])}, "
            f"quantity/lot size {contract['quantity']}"
        )

    st.header("2. VALIDATE")
    results = construction.get("validation_results", [])
    _show_rules(results)
    if not results:
        st.success("No validation findings.")
    if not construction.get("can_proceed"):
        st.error("Blocking validation prevents progression.")
        return

    st.header("3. CALCULATE")
    calculation = construction.get("calculation") or {}
    labels = (
        ("Combined premium", "combined_premium"),
        ("Total cost", "total_cost"),
        ("Upper break-even", "upper_break_even"),
        ("Lower break-even", "lower_break_even"),
        ("Maximum loss", "maximum_loss"),
        ("Required move points", "required_move_points"),
        ("Required move percentage", "required_move_percent"),
        ("Engine version", "engine_version"),
    )
    columns = st.columns(2)
    for index, (label, key) in enumerate(labels):
        columns[index % 2].metric(label, calculation.get(key, "—"))

    strategy_id = outcome.get("strategy_id")
    st.header("4. SIMULATE")
    st.warning(
        "EXPIRY PAYOFF SIMULATION — hypothetical expiry outcomes, not a price forecast."
    )
    lower = calculation.get("lower_break_even")
    strike = construction.get("selected_strike")
    upper = calculation.get("upper_break_even")
    default_prices = ", ".join(value for value in (lower, strike, upper) if value)
    custom = st.text_input("Scenario prices (comma-separated)", value=default_prices)
    if st.button("Run expiry simulation"):
        prices = [item.strip() for item in custom.split(",") if item.strip()]
        response = _call(
            client,
            "POST",
            f"/straddle/strategies/{strategy_id}/simulate",
            json={"scenario_prices": prices},
        )
        if response is not None:
            st.session_state["sl_simulation"] = response.json()
    simulation = st.session_state.get("sl_simulation")
    if simulation:
        st.dataframe(simulation.get("points", []), use_container_width=True)

    st.header("5. APPROVE")
    required = [
        item["rule_id"]
        for item in results
        if item.get("requires_acknowledgement")
    ]
    acknowledgements = set()
    for rule_id in required:
        if st.checkbox(f"Acknowledge {rule_id}", key=f"ack_{rule_id}"):
            acknowledgements.add(rule_id)
    st.write(f"Final estimated entry cost: {_money(calculation.get('total_cost'))}")
    if st.button("Approve strategy"):
        response = _call(
            client,
            "POST",
            f"/straddle/strategies/{strategy_id}/approve",
            json={"acknowledgements": sorted(acknowledgements)},
        )
        if response is not None:
            st.session_state["sl_approval"] = response.json()
            st.success("Strategy approved for paper trading.")

    if st.session_state.get("sl_approval"):
        st.header("6. PAPER TRADE")
        st.error("PAPER TRADING — NO LIVE ORDER WILL BE PLACED")
        position_id = st.text_input("Paper position ID (optional)")
        if st.button("OPEN PAPER POSITION", type="primary"):
            response = _call(
                client,
                "POST",
                f"/straddle/strategies/{strategy_id}/paper-position",
                json={"position_id": position_id or None},
            )
            if response is not None:
                opened = response.json()
                st.session_state["sl_position_id"] = opened["position"]["position_id"]
                st.success("Paper position opened. No broker order was sent.")


def _monitor_exit_review(client):
    st.header("7. MONITOR")
    response = _call(client, "GET", "/straddle/positions")
    if response is None:
        return
    positions = response.json()
    if not positions:
        st.caption("No paper positions yet.")
        return
    ids = [item["position"]["position_id"] for item in positions]
    preferred = st.session_state.get("sl_position_id")
    index = ids.index(preferred) if preferred in ids else 0
    position_id = st.selectbox("Paper position", ids, index=index)
    review_response = _call(client, "GET", f"/straddle/positions/{position_id}")
    if review_response is None:
        return
    review = review_response.json()
    position = review["paper_position"]
    closed = position["status"] == "CLOSED"
    if st.button("Refresh market valuation", disabled=closed):
        refreshed = _call(
            client, "POST", f"/straddle/positions/{position_id}/refresh"
        )
        if refreshed is not None:
            st.session_state["sl_latest_snapshot"] = refreshed.json()["monitoring"][
                "snapshot"
            ]
            st.rerun()
    snapshots = review.get("snapshots", [])
    snapshot = st.session_state.get("sl_latest_snapshot") or (
        snapshots[-1] if snapshots else None
    )
    if snapshot:
        metrics = (
            ("CALL price", snapshot.get("call_price")),
            ("PUT price", snapshot.get("put_price")),
            ("Spot", snapshot.get("spot_price")),
            ("Combined value", snapshot.get("combined_value")),
            ("Unrealised P&L", snapshot.get("unrealised_pnl")),
            ("Realised P&L", snapshot.get("realised_pnl")),
            ("Total P&L", snapshot.get("total_pnl")),
            ("CALL remaining", snapshot.get("remaining_call_quantity")),
            ("PUT remaining", snapshot.get("remaining_put_quantity")),
            ("Upper BE distance", snapshot.get("distance_to_upper_break_even")),
            ("Lower BE distance", snapshot.get("distance_to_lower_break_even")),
            ("Data status", snapshot.get("data_status")),
        )
        columns = st.columns(3)
        for idx, (label, value) in enumerate(metrics):
            columns[idx % 3].metric(label, value if value is not None else "Unavailable")
        total = snapshot.get("total_pnl")
        if total is not None:
            direction = "profit" if not str(total).startswith("-") else "loss"
            st.write(f"Current result is a {direction}: {_money(total)}")
        st.write(
            "Last-known-good valuation used: "
            + ("YES" if snapshot.get("used_last_known_good") else "NO")
        )
        for greek in ("net_delta", "net_gamma", "net_theta", "net_vega", "implied_volatility"):
            st.write(f"{greek}: {snapshot.get(greek) if snapshot.get(greek) is not None else 'Unavailable'}")

    st.header("8. EXIT")
    if closed:
        st.info("This position is CLOSED and read-only.")
    else:
        call_quantity = st.number_input(
            "CALL quantity to close",
            min_value=0,
            max_value=position["remaining_call_quantity"],
            value=position["remaining_call_quantity"],
            step=1,
        )
        put_quantity = st.number_input(
            "PUT quantity to close",
            min_value=0,
            max_value=position["remaining_put_quantity"],
            value=position["remaining_put_quantity"],
            step=1,
        )
        call_price = st.text_input("CALL exit premium")
        put_price = st.text_input("PUT exit premium")
        exit_reason = st.text_input("Exit reason", value="Manual paper exit")
        one_leg = (call_quantity > 0) != (put_quantity > 0)
        acknowledge = st.checkbox(
            "Acknowledge POS-001 one-leg exit risk",
            disabled=not one_leg,
        )
        if one_leg:
            st.warning("POS-001 requires acknowledgement before a one-leg exit.")
        if st.button("Apply simulated exit"):
            payload = {
                "call_quantity": call_quantity,
                "put_quantity": put_quantity,
                "call_exit_price": call_price or None,
                "put_exit_price": put_price or None,
                "exit_reason": exit_reason,
                "acknowledgements": ["POS-001"] if acknowledge else [],
            }
            exited = _call(
                client,
                "POST",
                f"/straddle/positions/{position_id}/exit",
                json=payload,
            )
            if exited is not None:
                result = exited.json()
                st.success(
                    f"Simulated exit applied. Realised P&L: "
                    f"{_money(result['position']['realised_pnl'])}"
                )
                st.rerun()

    st.header("9. REVIEW")
    st.subheader("Original strategy and calculations")
    st.json({key: review.get(key) for key in ("strategy", "calculations")})
    st.subheader("Warnings and acknowledgements")
    st.json(
        {
            "risk_assessments": review.get("risk_assessments"),
            "acknowledgements": review.get("acknowledgements"),
        }
    )
    st.subheader("Monitoring snapshots")
    st.dataframe(review.get("snapshots", []), use_container_width=True)
    st.subheader("Append-only event journal")
    st.dataframe(review.get("journal", []), use_container_width=True)
    if st.button("Prepare trade-review export"):
        export = _call(client, "GET", f"/straddle/positions/{position_id}/export")
        if export is not None:
            st.session_state[f"sl_export_{position_id}"] = export.content
    export_content = st.session_state.get(f"sl_export_{position_id}")
    if export_content is not None:
        st.download_button(
            "Download trade-review JSON",
            data=export_content,
            file_name=f"straddle-{position_id}.json",
            mime="application/json",
        )


def render_straddlelab(api_url: str) -> None:
    st.title("StraddleLab")
    st.error("PAPER TRADING ONLY — NO LIVE ORDERS ARE PLACED")
    st.caption(
        "Construct → Validate → Calculate → Simulate → Approve → "
        "Paper Trade → Monitor → Exit → Review"
    )
    with httpx.Client(base_url=api_url, timeout=30) as client:
        _construction(client)
        _validation_calculation(client)
        st.divider()
        _monitor_exit_review(client)

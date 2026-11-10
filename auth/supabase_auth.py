"""Supabase authentication helpers for SURYA.

Only the Supabase publishable/anon key belongs in Streamlit secrets.
Never place the service-role key in this application.
"""
from __future__ import annotations

from typing import Any

import streamlit as st
from supabase import create_client


def _read_config() -> tuple[str, str, str]:
    try:
        url = str(st.secrets["SUPABASE_URL"]).strip()
        key = str(st.secrets["SUPABASE_ANON_KEY"]).strip()
        app_url = str(st.secrets.get("APP_URL", "")).strip().rstrip("/")
    except Exception as exc:
        st.error(
            "SURYA authentication is not configured. Add SUPABASE_URL and "
            "SUPABASE_ANON_KEY to Streamlit secrets."
        )
        st.caption("Do not add the Supabase service-role key to this application.")
        st.stop()
        raise RuntimeError("Supabase secrets unavailable") from exc

    if not url or not key:
        st.error("SUPABASE_URL and SUPABASE_ANON_KEY must not be empty.")
        st.stop()
    return url, key, app_url


def _get_client():
    """One authenticated Supabase client per Streamlit browser session."""
    client = st.session_state.get("_surya_supabase_client")
    if client is None:
        url, key, _ = _read_config()
        client = create_client(url, key)
        st.session_state["_surya_supabase_client"] = client
    return client


def _user_from_response(response: Any):
    return getattr(response, "user", None)


def _complete_oauth_callback(client) -> None:
    """Exchange the Supabase PKCE authorization code for a session."""
    code = st.query_params.get("code")
    if not code:
        return
    try:
        response = client.auth.exchange_code_for_session(code)
        user = _user_from_response(response)
        if user is not None:
            st.session_state["surya_user"] = user
        # Remove the one-time code from the visible URL after exchange.
        st.query_params.clear()
        st.rerun()
    except Exception as exc:
        st.query_params.clear()
        st.error(f"Google sign-in could not be completed: {exc}")


def _current_user(client):
    try:
        response = client.auth.get_user()
        return getattr(response, "user", None)
    except Exception:
        return None


def _render_login(client, app_url: str) -> None:
    st.title("☀️ SURYA")
    st.subheader("Solar Park Master Planner")
    st.write("Sign in to access the solar park engineering workspace.")

    # Process OAuth callback before displaying the login controls.
    _complete_oauth_callback(client)

    tab_login, tab_signup, tab_reset = st.tabs(
        ["Sign in", "Create account", "Reset password"]
    )

    with tab_login:
        with st.form("surya_login_form"):
            email = st.text_input("Email address", key="surya_login_email")
            password = st.text_input(
                "Password", type="password", key="surya_login_password"
            )
            submitted = st.form_submit_button(
                "Sign in", type="primary", use_container_width=True
            )
        if submitted:
            try:
                response = client.auth.sign_in_with_password(
                    {"email": email.strip(), "password": password}
                )
                user = _user_from_response(response)
                if user is None:
                    st.error("Sign-in did not return a user. Please try again.")
                else:
                    st.session_state["surya_user"] = user
                    st.rerun()
            except Exception as exc:
                st.error(f"Sign-in failed: {exc}")

        st.markdown("**Or**")
        if not app_url:
            st.info(
                "Google sign-in requires APP_URL in Streamlit secrets, set to "
                "the public SURYA application URL."
            )
        elif st.button("Continue with Google", use_container_width=True):
            try:
                response = client.auth.sign_in_with_oauth(
                    {
                        "provider": "google",
                        "options": {"redirect_to": app_url},
                    }
                )
                oauth_url = getattr(response, "url", None)
                if oauth_url:
                    st.link_button(
                        "Continue to Google",
                        oauth_url,
                        use_container_width=True,
                    )
                else:
                    st.error("Supabase did not return a Google authorization URL.")
            except Exception as exc:
                st.error(f"Unable to start Google sign-in: {exc}")

    with tab_signup:
        with st.form("surya_signup_form"):
            new_email = st.text_input("Email address", key="surya_signup_email")
            new_password = st.text_input(
                "Password (minimum 8 characters)",
                type="password",
                key="surya_signup_password",
            )
            confirm_password = st.text_input(
                "Confirm password",
                type="password",
                key="surya_signup_confirm",
            )
            create_account = st.form_submit_button(
                "Create account", type="primary", use_container_width=True
            )
        if create_account:
            if not new_email.strip():
                st.error("Enter an email address.")
            elif len(new_password) < 8:
                st.error("Use a password with at least 8 characters.")
            elif new_password != confirm_password:
                st.error("The passwords do not match.")
            else:
                try:
                    response = client.auth.sign_up(
                        {"email": new_email.strip(), "password": new_password}
                    )
                    user = _user_from_response(response)
                    session = getattr(response, "session", None)
                    if session and user:
                        st.session_state["surya_user"] = user
                        st.success("Account created.")
                        st.rerun()
                    else:
                        st.success(
                            "Registration submitted. Check your email to verify "
                            "your account, then sign in."
                        )
                except Exception as exc:
                    st.error(f"Account registration failed: {exc}")

    with tab_reset:
        with st.form("surya_password_reset_form"):
            reset_email = st.text_input("Registered email address")
            send_reset = st.form_submit_button("Send password reset email")
        if send_reset:
            if not reset_email.strip():
                st.error("Enter your registered email address.")
            else:
                try:
                    client.auth.reset_password_for_email(
                        reset_email.strip(),
                        {"redirect_to": app_url} if app_url else {},
                    )
                    st.success("If the account exists, a password reset email was sent.")
                except Exception as exc:
                    st.error(f"Unable to send reset email: {exc}")


def require_authentication() -> Any:
    """Gate a Streamlit entrypoint/page and return the authenticated Supabase user."""
    try:
        client = _get_client()
    except Exception:
        # _read_config already renders a safe configuration error.
        st.stop()

    _complete_oauth_callback(client)
    user = _current_user(client)
    if user is None:
        st.session_state.pop("surya_user", None)
        _, _, app_url = _read_config()
        _render_login(client, app_url)
        st.stop()

    st.session_state["surya_user"] = user
    email = getattr(user, "email", None) or "Signed-in user"
    st.sidebar.caption(f"Signed in: {email}")
    if st.sidebar.button("Sign out", key="surya_sign_out"):
        try:
            client.auth.sign_out()
        finally:
            st.session_state.pop("surya_user", None)
            st.session_state.pop("_surya_supabase_client", None)
            st.rerun()
    return user

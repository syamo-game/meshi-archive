"""Configure an isolated Discord login environment before application imports."""
import os

os.environ.setdefault("ADMIN_USER_ID", "123456789012345678")
os.environ.setdefault("DISCORD_CLIENT_ID", "synthetic-test-client")
os.environ.setdefault("DISCORD_CLIENT_SECRET", "synthetic-test-secret")
os.environ.setdefault("DISCORD_REDIRECT_URI", "http://localhost/auth/discord/callback")

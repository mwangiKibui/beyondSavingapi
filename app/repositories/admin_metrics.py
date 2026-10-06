import asyncpg


async def get_admin_metrics(pool: asyncpg.Pool) -> dict:
    """Headline counts for the admin panel's metrics page: how many users
    and accounts exist, and which institutions (accounts.provider) are in
    use - the seed of "which institutions should we build direct
    integrations for next". Counts active accounts only, matching how the
    Accounts page itself counts them."""
    total_users = await pool.fetchval("SELECT COUNT(*) FROM users")
    total_accounts = await pool.fetchval("SELECT COUNT(*) FROM accounts WHERE is_active = true")

    institution_rows = await pool.fetch(
        """
        SELECT COALESCE(provider, 'Unknown') AS provider, COUNT(*) AS account_count
        FROM accounts
        WHERE is_active = true
        GROUP BY provider
        ORDER BY account_count DESC
        """
    )

    return {
        "total_users": total_users,
        "total_accounts": total_accounts,
        "accounts_by_institution": [dict(row) for row in institution_rows],
    }

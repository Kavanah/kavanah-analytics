.. _account-setup:

==============================
Account Setup & First Login
==============================

Session Duration
----------------

Your session uses an **idle timeout** model. As long as you're actively using
the dashboard, your session stays valid. If you walk away and don't make any
requests for the idle timeout (4 hours by default), your next request
redirects you to the login page.

There's also an absolute cap: regardless of activity, you'll be asked to
re-authenticate after the maximum session lifetime (24 hours by default).

----

First-Login Checklist
---------------------

After logging in for the first time:

1. **Confirm your data sources are connected.** Navigate to
   *Import Data* and check the data source status table. Green indicators mean
   a source is configured and reachable.

2. **Run your first import.** Select a platform, choose a date range (e.g. "Last 7 days"),
   and click **Run Import**. See :doc:`../management-console/import-data` for details.

3. **View a built-in dashboard.** Navigate to *Dashboards* and select a platform tab.

4. **Set up two-factor authentication** if your administrator has pre-configured MFA
   for your account but you have not yet enrolled. You will be prompted automatically
   on next login.

----

Password Reset
--------------

If you forget your password, click **Forgot password?** on the login
page. A reset link is emailed to your registered address.

If you don't receive the email within a few minutes, ask an admin to
reset it for you — superusers can issue a fresh temporary password
from the user detail sidebar in User Management. See
:doc:`/management-console/user-management` for the admin-side flow.

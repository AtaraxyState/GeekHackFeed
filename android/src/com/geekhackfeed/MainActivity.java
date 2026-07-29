package com.geekhackfeed;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.Menu;
import android.view.MenuItem;
import android.view.ViewGroup;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Toast;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Thin client for the geekhack feed server.
 *
 * The server does the scraping, classifying and thumbnailing on a timer; this
 * just renders whatever it is currently serving. That means improving the feed
 * only means restarting the server, never rebuilding the app.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "geekhack_feed";
    private static final String KEY_SERVER = "server_url";
    private static final int DEFAULT_PORT = 8765;
    /** Reload on resume if the page has been sitting for longer than this. */
    private static final long STALE_AFTER_MS = 90_000L;

    private static final int MENU_REFRESH = 1;
    private static final int MENU_RESCAN = 2;
    private static final int MENU_SERVER = 3;
    private static final int MENU_UPDATE = 4;

    private WebView web;
    private String server;
    private long loadedAt;
    private boolean loadFailed;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);

        web = new WebView(this);
        web.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(web);

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(false);
        settings.setBuiltInZoomControls(false);
        // Fall back to whatever was cached last time the server was reachable.
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                // Thread links point at geekhack; hand those to the browser.
                if (server != null && url.startsWith(server)) {
                    return false;
                }
                try {
                    startActivity(new android.content.Intent(
                            android.content.Intent.ACTION_VIEW, request.getUrl()));
                    return true;
                } catch (Exception e) {
                    return false;
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    loadFailed = true;
                    showError(error.getDescription() == null
                            ? "could not reach the server" : error.getDescription().toString());
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (!loadFailed) {
                    loadedAt = System.currentTimeMillis();
                }
            }
        });

        server = getPrefs().getString(KEY_SERVER, null);
        if (server == null) {
            promptForServer(true);
        } else {
            load();
        }

        // Throttled inside UpdateManager, so this is quiet on most launches.
        checkForUpdates(false);
    }

    // ---------------------------------------------------------------- server

    private SharedPreferences getPrefs() {
        return getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /**
     * Accepts "192.168.1.20", "192.168.1.20:9000" or a full URL and fills in
     * the parts the user left out.
     */
    static String normalise(String raw) {
        if (raw == null) {
            return null;
        }
        String value = raw.trim();
        if (value.isEmpty()) {
            return null;
        }
        if (!value.contains("://")) {
            value = "http://" + value;
        }
        while (value.endsWith("/")) {
            value = value.substring(0, value.length() - 1);
        }
        try {
            URL url = new URL(value);
            if (url.getPort() == -1 && "http".equals(url.getProtocol())) {
                value = url.getProtocol() + "://" + url.getHost() + ":" + DEFAULT_PORT
                        + url.getPath();
            }
        } catch (Exception e) {
            return null;
        }
        return value;
    }

    private void promptForServer(final boolean firstRun) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("192.168.1.20:" + DEFAULT_PORT);
        if (server != null) {
            input.setText(server);
        }

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        box.setPadding(pad, pad / 2, pad, 0);
        box.addView(input);

        new AlertDialog.Builder(this)
                .setTitle("Feed server")
                .setMessage("Address of the machine running serve.py")
                .setView(box)
                .setCancelable(!firstRun)
                .setPositiveButton("Connect", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        String value = normalise(input.getText().toString());
                        if (value == null) {
                            toast("That does not look like an address");
                            promptForServer(firstRun);
                            return;
                        }
                        server = value;
                        getPrefs().edit().putString(KEY_SERVER, value).apply();
                        load();
                    }
                })
                .show();
    }

    private void load() {
        if (server == null) {
            promptForServer(true);
            return;
        }
        if (!isOnline()) {
            showError("this device is offline");
            return;
        }
        loadFailed = false;
        web.loadUrl(server + "/");
    }

    @SuppressWarnings("deprecation") // getActiveNetworkInfo covers back to API 24
    private boolean isOnline() {
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            NetworkInfo info = cm.getActiveNetworkInfo();
            return info != null && info.isConnected();
        } catch (Exception e) {
            return true; // if we cannot tell, let the load attempt decide
        }
    }

    /** Ask the server to re-scrape now rather than waiting for its timer. */
    private void requestRescan() {
        if (server == null) {
            return;
        }
        toast("Asking the server to rescan...");
        final String url = server + "/api/refresh";
        new Thread(new Runnable() {
            @Override
            public void run() {
                String result;
                HttpURLConnection conn = null;
                try {
                    conn = (HttpURLConnection) new URL(url).openConnection();
                    conn.setConnectTimeout(10_000);
                    conn.setReadTimeout(180_000); // a full rescrape is not quick
                    InputStream in = conn.getInputStream();
                    while (in.read() != -1) {
                        // drain so the server sees a completed request
                    }
                    in.close();
                    result = "Server rescanned";
                } catch (Exception e) {
                    result = "Rescan failed: " + e.getMessage();
                } finally {
                    if (conn != null) {
                        conn.disconnect();
                    }
                }
                final String message = result;
                new Handler(Looper.getMainLooper()).post(new Runnable() {
                    @Override
                    public void run() {
                        toast(message);
                        load();
                    }
                });
            }
        }).start();
    }

    private void showError(String detail) {
        String safe = detail == null ? "" : detail.replace("<", "&lt;");
        String where = server == null ? "(no server set)" : server;
        String html = "<!doctype html><meta name='viewport' content='width=device-width,"
                + "initial-scale=1'><style>"
                + "body{margin:0;min-height:100vh;display:flex;align-items:center;"
                + "justify-content:center;background:#0e0f13;color:#e8eaf2;"
                + "font:15px/1.6 system-ui,sans-serif;padding:32px;text-align:center}"
                + "code{color:#7c8cff;word-break:break-all}"
                + "p{color:#949bb3;max-width:30em}</style>"
                + "<div><h2>Can't reach the feed server</h2>"
                + "<p><code>" + where + "</code></p>"
                + "<p>" + safe + "</p>"
                + "<p>Check that <code>serve.py</code> is running on that machine and that "
                + "the phone is on the same network. Use the menu to change the address.</p>"
                + "</div>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }

    // ------------------------------------------------------------------ menu

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, MENU_REFRESH, 0, "Refresh")
                .setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER);
        menu.add(0, MENU_RESCAN, 1, "Rescan geekhack now");
        menu.add(0, MENU_SERVER, 2, "Change server");
        menu.add(0, MENU_UPDATE, 3, "Check for updates");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case MENU_REFRESH:
                load();
                return true;
            case MENU_RESCAN:
                requestRescan();
                return true;
            case MENU_SERVER:
                promptForServer(false);
                return true;
            case MENU_UPDATE:
                checkForUpdates(true);
                return true;
            default:
                return super.onOptionsItemSelected(item);
        }
    }

    // -------------------------------------------------------------- updates

    /** @param manual true from the menu: report "up to date" and ignore throttling */
    private void checkForUpdates(final boolean manual) {
        if (manual) {
            toast("Checking for updates...");
        }
        UpdateManager.check(this, manual, new UpdateManager.Callback() {
            @Override
            public void onResult(UpdateManager.Release release, String error) {
                if (isFinishing()) {
                    return;
                }
                if (error != null) {
                    if (manual) {
                        toast("Update check failed: " + error);
                    }
                    return;
                }
                if (release == null) {
                    if (manual) {
                        toast("Up to date (" + UpdateManager.versionName(MainActivity.this) + ")");
                    }
                    return;
                }
                UpdateManager.promptAndInstall(MainActivity.this, release);
            }
        });
    }

    // --------------------------------------------------------------- plumbing

    @Override
    protected void onResume() {
        super.onResume();
        boolean stale = System.currentTimeMillis() - loadedAt > STALE_AFTER_MS;
        if (server != null && (loadFailed || (loadedAt > 0 && stale))) {
            load();
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}

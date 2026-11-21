package com.panfollow.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

/**
 * 极薄的 WebView 壳：加载 NAS 上的追更服务。
 * 所有界面逻辑都在网页里，这里只负责容器、错误页、服务器地址和返回键。
 */
public class MainActivity extends Activity {

    private static final String PREF = "cfg";
    private static final String KEY_URL = "server_url";
    private static final String DEFAULT_URL = "http://192.168.1.100:8099";
    private static final int BG = 0xFF111318;
    private static final int CARD = 0xFF191C23;
    private static final int FG = 0xFFE8EAF0;
    private static final int DIM = 0xFF8B93A7;
    private static final int ACCENT = 0xFF4F8CFF;

    private WebView web;
    private ProgressBar bar;
    private LinearLayout errBox;
    private TextView errText;
    private String serverUrl;

    private int dp(float v) {
        return Math.round(getResources().getDisplayMetrics().density * v);
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        SharedPreferences sp = getSharedPreferences(PREF, MODE_PRIVATE);
        serverUrl = sp.getString(KEY_URL, DEFAULT_URL);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(BG);

        web = buildWebView();
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));

        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(100);
        bar.setIndeterminate(false);
        bar.setProgressTintList(android.content.res.ColorStateList.valueOf(ACCENT));
        bar.setBackgroundColor(Color.TRANSPARENT);
        FrameLayout.LayoutParams blp = new FrameLayout.LayoutParams(-1, dp(3));
        blp.gravity = Gravity.TOP;
        root.addView(bar, blp);

        errBox = buildErrorBox();
        errBox.setVisibility(View.GONE);
        FrameLayout.LayoutParams elp = new FrameLayout.LayoutParams(-1, -1);
        root.addView(errBox, elp);

        TextView gear = buildGear();
        FrameLayout.LayoutParams glp = new FrameLayout.LayoutParams(dp(44), dp(44));
        glp.gravity = Gravity.TOP | Gravity.END;
        glp.topMargin = dp(6);
        glp.rightMargin = dp(4);
        root.addView(gear, glp);

        setContentView(root);

        if (Build.VERSION.SDK_INT >= 21) {
            Window w = getWindow();
            w.setStatusBarColor(BG);
            w.setNavigationBarColor(BG);
        }

        web.loadUrl(serverUrl);

        ensureNotifyPermission();
        startNotifyService();
    }

    /** Android 13+ 需要运行时授权通知权限 */
    private void ensureNotifyPermission() {
        if (Build.VERSION.SDK_INT >= 33) {
            if (checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                    != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1001);
            }
        }
    }

    private void startNotifyService() {
        try {
            Intent i = new Intent(this, NotifyService.class);
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(i);
            else startService(i);
        } catch (Throwable ignored) {
        }
    }

    // ---------- 视图 ----------

    private WebView buildWebView() {
        WebView v = new WebView(this);
        v.setBackgroundColor(BG);
        WebSettings s = v.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " KDocsApp/1.0");
        try {
            CookieManager.getInstance().setAcceptCookie(true);
            CookieManager.getInstance().setAcceptThirdPartyCookies(v, true);
        } catch (Throwable ignored) {
        }

        v.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int p) {
                bar.setProgress(p);
                bar.setVisibility(p >= 100 ? View.GONE : View.VISIBLE);
            }
        });

        v.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                String u = req.getUrl().toString();
                if (u.startsWith(serverUrl)) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u)));
                } catch (Throwable ignored) {
                }
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) {
                    showError("连不上 " + serverUrl + "\n\n" + err.getDescription()
                            + "\n\n检查手机和 NAS 是否在同一 Wi-Fi。");
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                bar.setVisibility(View.GONE);
                errBox.setVisibility(View.GONE);
            }
        });

        v.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public void requestNotify() {
                runOnUiThread(() -> ensureNotifyPermission());
            }

            @JavascriptInterface
            public String notifyStatus() {
                if (Build.VERSION.SDK_INT >= 33) {
                    return checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                            == android.content.pm.PackageManager.PERMISSION_GRANTED
                            ? "granted" : "denied";
                }
                return "granted";
            }

            @JavascriptInterface
            public void startService() {
                runOnUiThread(() -> startNotifyService());
            }

            @JavascriptInterface
            public String appVersion() {
                return "1.2";
            }
        }, "KDocsApp");

        return v;
    }

    private LinearLayout buildErrorBox() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(BG);
        box.setPadding(dp(28), dp(28), dp(28), dp(28));

        TextView icon = new TextView(this);
        icon.setText("⚠");
        icon.setTextSize(TypedValue.COMPLEX_UNIT_SP, 34);
        icon.setTextColor(0xFFE5A54B);
        icon.setGravity(Gravity.CENTER);
        box.addView(icon);

        errText = new TextView(this);
        errText.setTextColor(DIM);
        errText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        errText.setGravity(Gravity.CENTER);
        errText.setLineSpacing(dp(4), 1f);
        LinearLayout.LayoutParams tlp = new LinearLayout.LayoutParams(-1, -2);
        tlp.topMargin = dp(14);
        box.addView(errText, tlp);

        Button retry = new Button(this);
        retry.setText("重试");
        retry.setAllCaps(false);
        retry.setTextColor(Color.WHITE);
        GradientDrawable bg1 = new GradientDrawable();
        bg1.setColor(ACCENT);
        bg1.setCornerRadius(dp(12));
        retry.setBackground(bg1);
        LinearLayout.LayoutParams rlp = new LinearLayout.LayoutParams(-1, dp(46));
        rlp.topMargin = dp(22);
        retry.setOnClickListener(new View.OnClickListener() {
            public void onClick(View view) {
                errBox.setVisibility(View.GONE);
                web.loadUrl(serverUrl);
            }
        });
        box.addView(retry, rlp);

        Button cfg = new Button(this);
        cfg.setText("修改服务器地址");
        cfg.setAllCaps(false);
        cfg.setTextColor(FG);
        GradientDrawable bg2 = new GradientDrawable();
        bg2.setColor(CARD);
        bg2.setCornerRadius(dp(12));
        bg2.setStroke(dp(1), 0xFF282C36);
        cfg.setBackground(bg2);
        LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(-1, dp(46));
        clp.topMargin = dp(10);
        cfg.setOnClickListener(new View.OnClickListener() {
            public void onClick(View view) {
                askServer();
            }
        });
        box.addView(cfg, clp);

        return box;
    }

    private TextView buildGear() {
        TextView g = new TextView(this);
        g.setText("⚙");
        g.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17);
        g.setTextColor(DIM);
        g.setGravity(Gravity.CENTER);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(0x33222833);
        bg.setCornerRadius(dp(22));
        g.setBackground(bg);
        g.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                askServer();
            }
        });
        return g;
    }

    private void showError(String msg) {
        errText.setText(msg);
        errBox.setVisibility(View.VISIBLE);
        bar.setVisibility(View.GONE);
    }

    // ---------- 服务器地址 ----------

    private void askServer() {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(serverUrl);
        input.setSelectAllOnFocus(true);
        input.setTextColor(FG);
        input.setHint("http://192.168.1.100:8099");
        input.setPadding(dp(16), dp(12), dp(16), dp(12));

        new AlertDialog.Builder(this)
                .setTitle("服务器地址")
                .setMessage("填 NAS 上追更服务的地址，例如 http://192.168.1.100:8099")
                .setView(input)
                .setPositiveButton("保存并加载", (d, w) -> {
                    String v = input.getText().toString().trim();
                    if (!v.startsWith("http://") && !v.startsWith("https://")) v = "http://" + v;
                    while (v.endsWith("/")) v = v.substring(0, v.length() - 1);
                    serverUrl = v;
                    getSharedPreferences(PREF, MODE_PRIVATE).edit().putString(KEY_URL, v).apply();
                    errBox.setVisibility(View.GONE);
                    web.loadUrl(serverUrl);
                    try {
                        stopService(new Intent(MainActivity.this, NotifyService.class));
                    } catch (Throwable ignored) {
                    }
                    startNotifyService();
                })
                .setNegativeButton("取消", null)
                .show();
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}

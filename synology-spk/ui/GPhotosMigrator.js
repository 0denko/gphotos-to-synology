/**
 * Google Photos Migrator — Native Synology DSM 7 Desktop Window.
 *
 * Opens directly inside DSM (Ext JS 3 SYNO.SDS.AppWindow) and loads the
 * migrator dashboard over DSM's own HTTPS origin via proxy.cgi so it works
 * seamlessly over LAN HTTPS (:5001) and Synology QuickConnect with zero
 * mixed-content errors or extra browser tabs.
 */

Ext.ns("SYNO.SDS.GPhotosMigrator");

Ext.define("SYNO.SDS.GPhotosMigrator.Application", {
    extend: "SYNO.SDS.AppInstance",
    appWindowName: "SYNO.SDS.GPhotosMigrator.AppWindow",
    constructor: function () {
        this.callParent(arguments);
    }
});

Ext.define("SYNO.SDS.GPhotosMigrator.AppWindow", {
    extend: "SYNO.SDS.AppWindow",

    PROXY_URL: "/webman/3rdparty/gphotos-migrator/proxy.cgi",

    _resolveDsmSession: function () {
        try {
            var sess = (window.SYNO && SYNO.SDS && SYNO.SDS.Session) || null;
            if (!sess && window.parent && window.parent.SYNO && window.parent.SYNO.SDS) {
                sess = window.parent.SYNO.SDS.Session;
            }
            return sess || null;
        } catch (e) {
            return null;
        }
    },

    _resolveDsmUser: function () {
        var sess = this._resolveDsmSession();
        if (sess) {
            var candidate = sess.user || sess.account || sess.username || "";
            if (typeof candidate === "string" && candidate.trim()) {
                return candidate.trim();
            }
        }
        return "";
    },

    _resolveSynoToken: function () {
        var sess = this._resolveDsmSession();
        if (sess) {
            var token = sess.SynoToken || sess.synoToken || "";
            if (typeof token === "string" && token.trim()) {
                return token.trim();
            }
        }
        return "";
    },

    _buildIframeUrl: function () {
        var params = [];
        var user = this._resolveDsmUser();
        if (user) {
            params.push("dsm_user=" + encodeURIComponent(user));
        }
        var synoToken = this._resolveSynoToken();
        if (synoToken) {
            params.push("syno_token=" + encodeURIComponent(synoToken));
        }
        if (params.length > 0) {
            return this.PROXY_URL + "?" + params.join("&");
        }
        return this.PROXY_URL;
    },

    constructor: function (cfg) {
        cfg = cfg || {};
        var me = this;
        var iframeId = "gphotos-migrator-frame-" + Ext.id();
        var initialUrl = me._buildIframeUrl();

        var framePanel = new Ext.Panel({
            border: false,
            layout: "fit",
            bodyStyle: "background:#0b1120;overflow:hidden;",
            html:
                '<iframe id="' + iframeId + '" ' +
                'src="' + initialUrl + '" ' +
                'style="width:100%;height:100%;border:0;display:block;background:#0b1120;" ' +
                'allow="clipboard-write"></iframe>'
        });

        cfg = Ext.apply({
            title: "Google Photos Migrator",
            width: 1120,
            height: 740,
            minWidth: 760,
            minHeight: 520,
            resizable: true,
            maximizable: true,
            minimizable: true,
            layout: "fit",
            tbar: [
                {
                    xtype: "syno_button",
                    text: "Reload",
                    handler: function () {
                        var el = document.getElementById(iframeId);
                        if (el) {
                            el.src = me._buildIframeUrl();
                        }
                    }
                }
            ],
            items: [framePanel]
        }, cfg);

        this.callParent([cfg]);
    }
});

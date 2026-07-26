import AppKit
import WebKit
import Carbon.HIToolbox

private let lumenAppURL = URL(string: "https://lumenboard.org/desktop")!
private let lumenCallbackOrigin = URL(string: "https://lumenboard.org/auth/callback")!

final class LumenWindowController: NSWindowController, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private let webView: WKWebView

    init() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.preferences.setValue(true, forKey: "allowsPictureInPictureMediaPlayback")
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: "window.LumenDesktop = true;",
                injectionTime: .atDocumentStart,
                forMainFrameOnly: false
            )
        )

        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.customUserAgent = "LumenDesktop/1.1 (macOS)"
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Lumen"
        window.minSize = NSSize(width: 760, height: 560)
        window.center()
        window.contentView = webView
        window.titlebarAppearsTransparent = true

        super.init(window: window)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        configuration.userContentController.add(self, name: "lumenOpenExternal")
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func start() {
        webView.load(URLRequest(url: lumenAppURL))
    }

    func openAuthenticationCallback(_ deepLink: URL) {
        guard deepLink.scheme == "org.lumenboard.app",
              deepLink.host == "auth",
              deepLink.path == "/callback" else { return }
        var components = URLComponents(url: lumenCallbackOrigin, resolvingAgainstBaseURL: false)!
        let deepLinkComponents = URLComponents(url: deepLink, resolvingAgainstBaseURL: false)
        components.queryItems = deepLinkComponents?.queryItems
        components.fragment = deepLinkComponents?.fragment
        if let callback = components.url {
            window?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            webView.load(URLRequest(url: callback))
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "lumenOpenExternal",
              let rawURL = message.body as? String,
              let url = URL(string: rawURL),
              ["https", "http"].contains(url.scheme?.lowercased() ?? "") else { return }
        NSWorkspace.shared.open(url)
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if navigationAction.targetFrame == nil {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    @available(macOS 12.0, *)
    func webView(
        _ webView: WKWebView,
        requestMediaCapturePermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        type: WKMediaCaptureType,
        decisionHandler: @escaping (WKPermissionDecision) -> Void
    ) {
        decisionHandler(origin.host == "lumenboard.org" ? .grant : .deny)
    }
}

final class LumenAppDelegate: NSObject, NSApplicationDelegate {
    private var windowController: LumenWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        windowController = LumenWindowController()
        windowController?.showWindow(nil)

        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleGetURL(event:reply:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
        windowController?.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        NSAppleEventManager.shared().removeEventHandler(
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    @objc private func handleGetURL(event: NSAppleEventDescriptor, reply: NSAppleEventDescriptor) {
        guard let rawURL = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue,
              let url = URL(string: rawURL) else { return }
        windowController?.openAuthenticationCallback(url)
    }
}

let application = NSApplication.shared
let delegate = LumenAppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()

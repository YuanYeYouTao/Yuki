import { useQuery } from "./hooks";
import { useEffect, useState } from "react";
import { login, logout, restoreSession } from "./api";
import type { Page, Row, Session } from "./api";
import { ActionSheet } from "./actions";
import type { Intent } from "./actions";
import { Empty, ErrorNote, Icon } from "./components";
import { Avatar, useDisplayNames } from "./names";
import { ThemePicker } from "./theme";
import { Chat } from "./chat";
import {
  Assets,
  Audit,
  Autonomy,
  Files,
  Health,
  Identity,
  Memory,
  Models,
  Persona,
  Notebook,
  SettingsPage,
  Tools,
  Work,
} from "./pages";
import type { PageProps } from "./pages";

const navigation = [
  ["overview", "house", "手帐", "read_system"],
  ["chat", "chat-bubble", "聊天记录", "list_chat_events"],
  ["autonomy", "moon-star", "自主活动", "read_participation"],
  ["work", "alarm-clock", "工作与定时", "list_work"],
  ["models", "gear", "模型与用量", "list_model_usage"],
  ["persona", "fountain-pen", "人格提示词", "read_config_file"],
  ["memory", "notebook", "记忆", "list_memory_facts"],
  ["tools", "box", "插件", "list_plugins"],
  ["files", "folder", "工作区", "read_environment"],
  ["identity", "cat", "身份与连接", "list_persons"],
  ["settings", "fountain-pen", "配置", "list_effective_configs"],
  ["assets", "flower", "表情与语音", "list_emoji_assets"],
  ["audit", "scroll", "轨迹与审计", "list_execution_trace"],
];

function SignIn({ signedIn }: { signedIn: (session: Session) => void }) {
  const [credential, setCredential] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  async function submit() {
    setBusy(true);
    setError(null);
    try {
      signedIn(await login(credential));
      setCredential("");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-wrap">
      <form
        className="login-card"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Icon name="key" />
        <h1>翻开 Yuki 的手帐</h1>
        <p>看看她最近在聊什么，做了哪些工作。</p>
        <label className="form-group" htmlFor="credential">
          管理凭据
          <input
            id="credential"
            className="form-control"
            type="password"
            autoComplete="current-password"
            required
            minLength={32}
            maxLength={4096}
            value={credential}
            onChange={(e) => setCredential(e.target.value)}
          />
        </label>
        {error != null && <ErrorNote error={error} />}
        <button className="btn-primary" disabled={busy}>
          {busy ? "正在打开…" : "进入"}
        </button>
        <p className="login-foot">凭据不保存到浏览器；会话过期后重新登录。</p>
      </form>
    </div>
  );
}

function Console({
  session,
  signedOut,
}: {
  session: Session;
  signedOut: () => void;
}) {
  const [address, setAddress] = useState(location.hash.slice(1) || "overview"),
    [refresh, setRefresh] = useState(0),
    [chosenConversation, setConversation] = useState("");
  const [intent, setIntent] = useState<Intent | null>(null),
    [error, setError] = useState<unknown>(null);
  const route = address.split("?")[0];
  const [conversationPage, setConversationPage] = useState(1);
  const [conversationJump, setConversationJump] = useState("1");
  const [chosenConversationRow, setChosenConversationRow] =
    useState<Row | null>(null);
  const [collectionRefresh, setCollectionRefresh] = useState(refresh);
  if (collectionRefresh !== refresh) {
    setCollectionRefresh(refresh);
    setConversationPage(1);
    setConversationJump("1");
  }
  const allowed = (method: string) =>
    !!session.surface.methods.find(
      (item) => item.name === method && item.authorized,
    );
  const conversations = useQuery<Page>(
    "list_conversations",
    { page: { limit: 100, number: conversationPage } },
    refresh,
    allowed("list_conversations"),
  );
  const spaces = useQuery<Page>(
    "list_spaces",
    { page: { limit: 100 } },
    refresh,
    allowed("list_spaces"),
  );
  const persons = useQuery<Page>(
    "list_persons",
    { page: { limit: 100 } },
    refresh,
    allowed("list_persons"),
  );
  useEffect(() => {
    const changed = () => {
      setAddress(location.hash.slice(1) || "overview");
    };
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  const loadedConversations = conversations.data?.items || [];
  const conversationRows =
    chosenConversationRow &&
    !loadedConversations.some(
      (row) => row.conversation_id === chosenConversationRow.conversation_id,
    )
      ? [chosenConversationRow, ...loadedConversations]
      : loadedConversations;
  const conversationPages = Math.max(
    1,
    Math.ceil((conversations.data?.total || 0) / 100),
  );
  const displayNames = useDisplayNames({
    conversation: conversationRows.map((row) =>
      String(row.conversation_id || ""),
    ),
    person: conversationRows.map((row) => String(row.person_id || "")),
    space: conversationRows.map((row) => String(row.space_id || "")),
  });
  const conversation =
    chosenConversation ||
    String(
      conversationRows.find((row) => row.conversation_id)?.conversation_id ||
        "",
    );
  if (!chosenConversation && conversation) {
    setConversation(conversation);
    setChosenConversationRow(
      conversationRows.find((row) => row.conversation_id === conversation) ||
        null,
    );
  }
  const dashboard = ["overview", "chat"].includes(route);
  useEffect(() => {
    document.body.className = dashboard ? "" : "has-settings-sidebar";
    document.title = `${navigation.find(([id]) => id === route)?.[2] || "手帐"} · Yuki`;
    return () => {
      document.body.className = "";
    };
  }, [dashboard, route]);
  const props: PageProps = {
    allowed,
    act: setIntent,
    refresh,
    conversation,
    chatContent: session.content_access.chat,
  };
  const pages: Record<string, () => React.ReactNode> = {
    autonomy: () => <Autonomy {...props} />,
    work: () => <Work {...props} />,
    models: () => <Models {...props} />,
    persona: () => <Persona {...props} />,
    memory: () => <Memory {...props} />,
    tools: () => <Tools {...props} />,
    files: () => <Files {...props} />,
    identity: () => <Identity {...props} />,
    settings: () => <SettingsPage {...props} />,
    assets: () => <Assets {...props} />,
    audit: () => <Audit {...props} />,
  };
  function name(row: Row): string {
    const owner = row.space_id
      ? spaces.data?.items.find((item) => item.space_id === row.space_id)
      : persons.data?.items.find((item) => item.person_id === row.person_id);
    const candidates = [
      displayNames[String(row.conversation_id)],
      displayNames[String(row.space_id || row.person_id)],
      owner?.name,
      owner?.display_name,
    ];
    return String(
      candidates.find((value) => value && value !== "redacted") ||
        (row.space_id ? "未命名群聊" : "未命名私聊"),
    );
  }
  async function signOut() {
    try {
      await logout();
      signedOut();
    } catch (e) {
      setError(e);
    }
  }
  return (
    <>
      <a
        className="skip"
        href="#main"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main")?.focus();
        }}
      >
        跳到内容
      </a>
      <header className="header">
        <div className="header-status">
          <span className="online-dot" title="管理会话已验证" />
          <span>{navigation.find(([id]) => id === route)?.[2] || "手帐"}</span>
        </div>
        <div className="header-right">
          <ThemePicker />
          <a className="nav-link" href="#overview">
            <Icon name="house" />
            手帐
          </a>
          <a className="nav-link" href="#models">
            <Icon name="gear" />
            设置
          </a>
          <a className="nav-link" href="#audit">
            <Icon name="scroll" />
            记录
          </a>
          <button
            className="header-button"
            aria-label="刷新数据"
            onClick={() => setRefresh(refresh + 1)}
          >
            <Icon name="arrows-refresh" />
          </button>
          <button className="header-button" onClick={signOut}>
            退出
          </button>
        </div>
      </header>
      <nav className="quick-row">
        <span className="conversation-avatar">
          <Avatar
            kind="conversation"
            id={conversation}
            label="当前会话头像"
            fallback={name(
              conversationRows.find(
                (row) => row.conversation_id === conversation,
              ) || {},
            )}
          />
        </span>
        <label htmlFor="conversation">会话</label>
        <select
          id="conversation"
          value={conversation}
          onChange={(e) => {
            setConversation(e.target.value);
            setChosenConversationRow(
              conversationRows.find(
                (row) => row.conversation_id === e.target.value,
              ) || null,
            );
          }}
        >
          <option value="">选择会话</option>
          {conversationRows
            .filter((row) => row.conversation_id)
            .map((row) => (
              <option
                key={String(row.conversation_id)}
                value={String(row.conversation_id)}
              >
                {name(row)}
              </option>
            ))}
        </select>
        {allowed("list_conversations") && (
          <div className="pagination" aria-label="会话分页">
            <button
              className="btn-secondary"
              disabled={conversationPage <= 1 || conversations.loading}
              onClick={() => {
                setConversationPage(conversationPage - 1);
                setConversationJump(String(conversationPage - 1));
              }}
            >
              上一页
            </button>
            <span className="small">
              第 {conversationPage} 页 / 共 {conversationPages} 页 · 共{" "}
              {conversations.data?.total ?? "?"} 个会话
            </span>
            <button
              className="btn-secondary"
              disabled={
                conversations.data?.total == null ||
                conversationPage >= conversationPages ||
                conversations.loading
              }
              onClick={() => {
                setConversationPage(conversationPage + 1);
                setConversationJump(String(conversationPage + 1));
              }}
            >
              下一页
            </button>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                const target = Number(conversationJump);
                if (
                  Number.isInteger(target) &&
                  target >= 1 &&
                  target <= conversationPages
                )
                  setConversationPage(target);
              }}
            >
              <label>
                跳至{" "}
                <input
                  aria-label="会话页码"
                  type="number"
                  min="1"
                  max={conversationPages}
                  value={conversationJump}
                  onChange={(event) => setConversationJump(event.target.value)}
                />
              </label>
              <button className="btn-secondary">跳转</button>
            </form>
          </div>
        )}
        {[
          ["chat", "chat-bubble", "聊天记录"],
          ["autonomy", "moon-star", "自主活动"],
          ["work", "alarm-clock", "工作与定时"],
          ["files", "folder", "工作区"],
        ].map(([id, icon, label]) => (
          <a key={id} href={`#${id}`} className="nav-link">
            <Icon name={icon} />
            {label}
          </a>
        ))}
      </nav>
      {!dashboard && (
        <aside className="settings-sidebar" aria-label="页面导航">
          <div className="settings-sidebar-title">页面与设置</div>
          <div className="settings-nav-group">
            <div className="settings-nav-group-title">Yuki 的手帐</div>
            {navigation.map(([id, icon, label]) => (
              <a
                key={id}
                href={`#${id}`}
                className={`settings-side-link ${route === id ? "active" : ""}`}
                aria-current={route === id ? "page" : undefined}
              >
                <span className="settings-side-icon">
                  <Icon name={icon} />
                </span>
                <span>{label}</span>
              </a>
            ))}
          </div>
        </aside>
      )}
      <main id="main" tabIndex={-1}>
        {error != null && <ErrorNote error={error} />}
        {conversations.error != null && (
          <ErrorNote error={conversations.error} />
        )}
        {dashboard ? (
          <Chat
            key={conversation}
            conversation={conversation}
            content={session.content_access.chat}
            refresh={refresh}
            notebook={<Notebook props={props} />}
          />
        ) : (
          <div className="settings-container">
            <div className="settings-page-intro">
              <h1>
                {navigation.find(([id]) => id === route)?.[2] || "页面不存在"}
              </h1>
            </div>
            {navigation.find(([id]) => id === route) &&
            !allowed(navigation.find(([id]) => id === route)![3]) ? (
              <Empty>当前账号未授予该页面的读取权限。</Empty>
            ) : (
              pages[route]?.() || (
                <Health
                  refresh={refresh}
                  conversation={conversation}
                  traceContent={allowed("read_execution_trace")}
                />
              )
            )}
          </div>
        )}
      </main>
      {intent && (
        <ActionSheet
          key={
            intent.method +
            String(
              intent.payload.resource_id ||
                intent.payload.key ||
                intent.target?.id,
            )
          }
          intent={intent}
          close={() => setIntent(null)}
          completed={() => setRefresh((value) => value + 1)}
        />
      )}
    </>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null),
    [checking, setChecking] = useState(true);
  useEffect(() => {
    let active = true;
    restoreSession()
      .then((value) => {
        if (active) setSession(value);
      })
      .catch(() => {
        /* Normal signed-out state. */
      })
      .finally(() => {
        if (active) setChecking(false);
      });
    const expired = () => setSession(null);
    window.addEventListener("session-expired", expired);
    return () => {
      active = false;
      window.removeEventListener("session-expired", expired);
    };
  }, []);
  return checking ? (
    <Empty>正在打开手帐…</Empty>
  ) : session ? (
    <Console session={session} signedOut={() => setSession(null)} />
  ) : (
    <>
      <header className="header login-header">
        <div className="header-right">
          <ThemePicker />
        </div>
      </header>
      <SignIn signedIn={setSession} />
    </>
  );
}

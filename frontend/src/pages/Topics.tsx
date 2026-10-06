// The filing system: the topic tree and the flat tags that sit beside it.
// Nothing here pretends a deletion is safe - the server refuses to remove a topic
// or a tag that questions still carry, and the reason is shown as it arrives.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { tagsApi, topicsApi, type TopicNode } from "../api/questions";

interface Actions {
  create: (body: { name: string; parent_id: string | null }) => void;
  rename: (id: string, name: string) => void;
  remove: (id: string) => void;
}

export default function Topics() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [message, setMessage] = useState<string | null>(null);

  const tree = useQuery({ queryKey: ["topics"], queryFn: topicsApi.list });
  const tags = useQuery({ queryKey: ["tags"], queryFn: tagsApi.list });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["topics"] });
    qc.invalidateQueries({ queryKey: ["tags"] });
    qc.invalidateQueries({ queryKey: ["questions"] });
  };
  const failed = (e: ApiError) => setMessage(e.message);

  const topicMutation = useMutation({
    mutationFn: async (op: { kind: "create" | "rename" | "remove"; id?: string; name?: string; parent_id?: string | null }) => {
      if (op.kind === "create") return topicsApi.create({ name: op.name as string, parent_id: op.parent_id ?? null });
      if (op.kind === "rename") return topicsApi.update(op.id as string, { name: op.name });
      return topicsApi.remove(op.id as string);
    },
    onSuccess: refresh,
    onError: failed,
  });
  const tagMutation = useMutation({
    mutationFn: async (op: { kind: "create" | "remove"; id?: string; name?: string }) =>
      op.kind === "create" ? tagsApi.create({ name: op.name as string }) : tagsApi.remove(op.id as string),
    onSuccess: refresh,
    onError: failed,
  });

  const actions: Actions = {
    create: (body) => topicMutation.mutate({ kind: "create", name: body.name, parent_id: body.parent_id ?? null }),
    rename: (id, name) => topicMutation.mutate({ kind: "rename", id, name }),
    remove: (id) => topicMutation.mutate({ kind: "remove", id }),
  };

  return (
    <div className="stack">
      <h1>{t("topics.title")}</h1>
      {message ? <div className="alert error">{message}</div> : null}

      <div className="two-col">
        <div className="card stack">
          <h2 style={{ margin: 0 }}>{t("topics.tree")}</h2>
          <p className="small muted">{t("topics.tree_hint")}</p>
          <NameForm label={t("topics.new_root")} onSubmit={(name) => actions.create({ name, parent_id: null })} />
          {(tree.data?.items || []).map((node) => (
            <TopicBranch key={node.id} node={node} depth={0} actions={actions} />
          ))}
          {!tree.isLoading && (tree.data?.items.length ?? 0) === 0 ? <p className="muted">{t("topics.empty")}</p> : null}
        </div>

        <div className="card stack">
          <h2 style={{ margin: 0 }}>{t("topics.tags")}</h2>
          <p className="small muted">{t("topics.tags_hint")}</p>
          <NameForm label={t("topics.new_tag")} onSubmit={(name) => tagMutation.mutate({ kind: "create", name })} />
          <div className="row" style={{ flexWrap: "wrap" }}>
            {(tags.data?.items || []).map((tag) => (
              <span className="chip" key={tag.id}>
                {tag.name}
                <span className="small muted"> · {tag.question_count}</span>
                <button
                  type="button"
                  className="chip-x"
                  onClick={() => tagMutation.mutate({ kind: "remove", id: tag.id })}
                  aria-label={t("topics.delete")}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
          {(tags.data?.items.length ?? 0) === 0 && !tags.isLoading ? <p className="muted">{t("topics.no_tags")}</p> : null}
        </div>
      </div>
    </div>
  );
}

function NameForm({ label, onSubmit }: { label: string; onSubmit: (name: string) => void }) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  return (
    <form
      className="row"
      onSubmit={(e) => {
        e.preventDefault();
        if (!value.trim()) return;
        onSubmit(value.trim());
        setValue("");
      }}
    >
      <input className="input" placeholder={label} value={value} onChange={(e) => setValue(e.target.value)} aria-label={label} />
      <button className="btn secondary" type="submit" disabled={!value.trim()}>
        {t("common.add")}
      </button>
    </form>
  );
}

function TopicBranch({ node, depth, actions }: { node: TopicNode; depth: number; actions: Actions }) {
  const { t } = useTranslation();
  const [renaming, setRenaming] = useState(false);
  const [adding, setAdding] = useState(false);

  return (
    <div className="branch">
      <div className="row">
        {renaming ? (
          <NameForm
            label={node.name}
            onSubmit={(name) => {
              actions.rename(node.id, name);
              setRenaming(false);
            }}
          />
        ) : (
          <>
            <strong>{node.name}</strong>
            <span className="small muted">
              {node.question_count} {t("topics.questions")}
            </span>
            <span className="spacer" />
            <button className="btn ghost" onClick={() => setRenaming(true)}>
              {t("topics.rename")}
            </button>
            <button className="btn ghost" onClick={() => setAdding((open) => !open)}>
              {t("topics.add_subtopic")}
            </button>
            <button className="btn ghost" onClick={() => actions.remove(node.id)}>
              {t("topics.delete")}
            </button>
          </>
        )}
      </div>
      {adding ? (
        <NameForm
          label={t("topics.new_subtopic")}
          onSubmit={(name) => {
            actions.create({ name, parent_id: node.id });
            setAdding(false);
          }}
        />
      ) : null}
      {node.children.map((child) => (
        <TopicBranch key={child.id} node={child} depth={depth + 1} actions={actions} />
      ))}
    </div>
  );
}

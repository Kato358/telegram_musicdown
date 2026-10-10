<script lang="ts">
  /** 日志页（FR-WEB-06）：运行日志尾部（app.log 只保留最近 500 行）+ 连接期间的实时错误（SSE）。
   *
   * 事实源分层：历史看文件（/api/logs 尾部窗口，本页轮询），「刚刚发生了什么」看
   * events.errors（编码规范 §5：唯一订阅点是 events store）。级别与关键字筛选在
   * 前端做——服务端每次返回整个尾部窗口，翻页签不必重新打 API。
   */
  import { onMount } from "svelte";
  import { t } from "$lib/i18n/index.svelte";
  import { api, errorText } from "$lib/api/client";
  import type { LogsResponse } from "$lib/api/types";
  import { events } from "$lib/stores/events.svelte";
  import { TONE_TEXT, logLevelTone } from "$lib/tone";
  import { formatSize } from "$lib/format";
  import CircleAlertIcon from "@lucide/svelte/icons/circle-alert";
  import DownloadIcon from "@lucide/svelte/icons/download";
  import FileTextIcon from "@lucide/svelte/icons/file-text";
  import RefreshCwIcon from "@lucide/svelte/icons/refresh-cw";
  import ScrollTextIcon from "@lucide/svelte/icons/scroll-text";
  import SearchIcon from "@lucide/svelte/icons/search";
  import Trash2Icon from "@lucide/svelte/icons/trash-2";
  import { Button } from "$lib/components/ui/button";
  import ConfirmDialog from "$lib/components/app/ConfirmDialog.svelte";
  import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
  } from "$lib/components/ui/select";
  import EmptyState from "$lib/components/app/EmptyState.svelte";
  import FilterTabs from "$lib/components/app/FilterTabs.svelte";
  import Note from "$lib/components/app/Note.svelte";
  import PageHeader from "$lib/components/app/PageHeader.svelte";
  import SectionCard from "$lib/components/app/SectionCard.svelte";

  const LEVEL_TABS = ["all", "error", "warning", "info", "debug"] as const;

  let data = $state<LogsResponse | null>(null);
  let loading = $state(false);
  let loadError = $state("");
  let file = $state("app.log");
  let level = $state<string>("all");
  let query = $state("");
  let auto = $state(true);
  let clearOpen = $state(false);
  let clearing = $state(false);

  let scroller: HTMLDivElement | undefined = $state();
  let stickBottom = $state(true);

  /** 级别归一：CRITICAL 并入 error 页签，其余用小写原样。 */
  function tabOf(levelName: string): string {
    const up = levelName.toUpperCase();
    return up === "CRITICAL" ? "error" : up.toLowerCase();
  }

  const entries = $derived(data?.entries ?? []);
  const filtered = $derived.by(() => {
    const q = query.trim().toLowerCase();
    return entries.filter((e) => {
      if (level !== "all" && tabOf(e.level) !== level) return false;
      if (q === "") return true;
      return e.message.toLowerCase().includes(q) || e.logger.toLowerCase().includes(q);
    });
  });
  const counts = $derived.by(() => {
    const map: Record<string, number> = {};
    for (const e of entries) {
      const key = tabOf(e.level);
      map[key] = (map[key] ?? 0) + 1;
    }
    map.all = entries.length;
    return map;
  });

  /** 每行稳定 key：内容 + 同内容行的出现序号——app.log 里有逐字重复的行，
   *  纯内容 key 会撞 Svelte 的 each_key_duplicate；追加场景下旧行 key 不变。 */
  const rows = $derived.by(() => {
    const seen: Record<string, number> = {};
    return filtered.map((e) => {
      const base = `${e.ts}|${e.level}|${e.logger}|${e.message}`;
      const n = (seen[base] = (seen[base] ?? 0) + 1);
      return { ...e, key: n === 1 ? base : `${base}#${n}` };
    });
  });

  function timeOf(ts: number): string {
    return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  }

  async function load() {
    loading = true;
    try {
      data = await api.get<LogsResponse>(`/api/logs?file=${encodeURIComponent(file)}`);
      loadError = "";
    } catch (err) {
      loadError = errorText(err, t("logs.loadFailed"));
    } finally {
      loading = false;
    }
  }

  async function clearLog() {
    clearing = true;
    try {
      await api.delete("/api/logs");
      await load();
    } catch (err) {
      loadError = errorText(err, t("logs.loadFailed"));
    } finally {
      clearing = false;
      clearOpen = false;
    }
  }

  function onScroll() {
    if (!scroller) return;
    stickBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48;
  }

  $effect(() => {
    // 换文件立即重取；file 是唯一 fetch 依赖，筛选都在前端
    void file;
    void load();
  });

  $effect(() => {
    if (!auto) return;
    const id = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(id);
  });

  $effect(() => {
    // 贴底跟随新日志；用户往上翻过就不拽人（滚回底部附近恢复）
    const n = filtered.length;
    if (!scroller || n === 0 || !stickBottom) return;
    scroller.scrollTop = scroller.scrollHeight;
  });

  onMount(() => {
    void load();
  });
</script>

<PageHeader title={t("logs.title")} lede={t("logs.lede")}>
  {#snippet aside()}
    <span class="tabular text-caption text-muted-foreground">
      {t("logs.count", { n: events.errors.length })}
    </span>
    <Button
      variant="ghost"
      disabled={events.errors.length === 0}
      onclick={() => events.clearErrors()}
    >
      {t("logs.clear")}
    </Button>
  {/snippet}
</PageHeader>

<SectionCard
  title={t("logs.serverSection")}
  hint={data
    ? t("logs.serverHint", {
        name: data.active_file,
        size: formatSize(data.file_size),
      })
    : undefined}
  icon={ScrollTextIcon}
>
  {#snippet actions()}
    <Button
      variant={auto ? "secondary" : "ghost"}
      size="sm"
      aria-pressed={auto}
      onclick={() => (auto = !auto)}
    >
      {t("logs.autoRefresh")}
    </Button>
    <Button variant="ghost" size="icon-sm" disabled={loading} onclick={() => void load()}>
      <RefreshCwIcon class="size-3.5" aria-hidden="true" />
      <span class="sr-only">{t("logs.refresh")}</span>
    </Button>
    <Button
      variant="ghost"
      size="icon-sm"
      href={`/api/logs/download?file=${encodeURIComponent(file)}`}
    >
      <DownloadIcon class="size-3.5" aria-hidden="true" />
      <span class="sr-only">{t("logs.download")}</span>
    </Button>
    <Button variant="ghost" size="icon-sm" onclick={() => (clearOpen = true)}>
      <Trash2Icon class="size-3.5" aria-hidden="true" />
      <span class="sr-only">{t("logs.clearLogs")}</span>
    </Button>
  {/snippet}

  <div class="flex flex-col gap-3">
    <div class="flex flex-wrap items-center gap-2">
      <div
        class="flex h-9 min-w-44 flex-1 items-center gap-2 rounded-full border border-border bg-surface-subtle px-3.5"
      >
        <SearchIcon class="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <input
          bind:value={query}
          type="search"
          class="min-w-0 flex-1 bg-transparent text-body outline-none placeholder:text-muted-foreground"
          placeholder={t("logs.searchPlaceholder")}
          aria-label={t("logs.searchPlaceholder")}
        />
      </div>
      <Select type="single" bind:value={file}>
        <SelectTrigger class="h-9 w-auto min-w-36 gap-1.5" aria-label={t("logs.file")}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {#each data?.files ?? [] as f (f.name)}
            <SelectItem value={f.name}>{f.name}</SelectItem>
          {/each}
        </SelectContent>
      </Select>
    </div>

    <div class="flex flex-wrap items-center gap-2">
      <FilterTabs
        label={t("logs.levelLabel")}
        items={LEVEL_TABS.map((k) => ({
          key: k,
          label: t(`logs.levels.${k}`),
          count: counts[k] ?? 0,
        }))}
        value={level}
        onchange={(k) => (level = k)}
      />
      <span class="tabular ml-auto text-caption text-muted-foreground">
        {t("logs.entries", { n: filtered.length })}
      </span>
    </div>

    {#if loadError}
      <Note tone="fail">{loadError}</Note>
    {:else if data?.truncated}
      <Note tone="wait">{t("logs.truncated")}</Note>
    {/if}

    {#if filtered.length === 0}
      <EmptyState
        bare
        icon={FileTextIcon}
        title={loadError === "" && entries.length === 0 ? t("logs.emptyFile") : t("logs.noMatch")}
        hint={loadError === "" && entries.length === 0 ? t("logs.emptyFileHint") : undefined}
      />
    {:else}
      <div
        bind:this={scroller}
        onscroll={onScroll}
        class="max-h-[480px] overflow-y-auto rounded-chip border border-border"
      >
        <ul class="flex flex-col p-1">
          {#each rows as e (e.key)}
            <li class="flex items-start gap-3 rounded-chip px-2 py-1.5 ui-transition hover:bg-rule">
              <span class="tabular shrink-0 pt-px text-caption text-faint-foreground">
                {timeOf(e.ts)}
              </span>
              <span class="w-14 shrink-0 pt-px text-caption font-medium {TONE_TEXT[logLevelTone(e.level)]}">
                {e.level}
              </span>
              <div class="min-w-0 flex-1">
                <span class="text-caption text-muted-foreground">{e.logger}</span>
                <p class="text-code whitespace-pre-wrap break-words">{e.message}</p>
              </div>
            </li>
          {/each}
        </ul>
      </div>
    {/if}
  </div>
</SectionCard>

<SectionCard
  title={t("logs.errorSection")}
  icon={CircleAlertIcon}
  badge={events.errors.length}
>
  {#snippet actions()}
    <Button
      variant="ghost"
      size="sm"
      disabled={events.errors.length === 0}
      onclick={() => events.clearErrors()}
    >
      {t("logs.clear")}
    </Button>
  {/snippet}

  {#if events.errors.length === 0}
    <EmptyState bare icon={CircleAlertIcon} title={t("logs.empty")} />
  {:else}
    <ul class="flex max-h-[360px] flex-col overflow-y-auto">
      {#each events.errors as entry (entry.id)}
        <li class="flex items-start gap-3 rounded-chip px-2 py-2 ui-transition hover:bg-rule">
          <span class="tabular shrink-0 text-caption text-faint-foreground">
            {timeOf(entry.ts)}
          </span>
          <span class="min-w-0 flex-1 text-code text-destructive-text break-all">
            {entry.message}
          </span>
        </li>
      {/each}
    </ul>
  {/if}
</SectionCard>

<ConfirmDialog
  bind:open={clearOpen}
  title={t("logs.clearTitle")}
  description={t("logs.clearBody")}
  target={{
    name: "app.log",
    meta: t("logs.clearTargetMeta", { n: events.errors.length }),
    mono: true,
  }}
  confirmLabel={t("logs.clearConfirm")}
  pendingLabel={t("logs.clearPending")}
  pending={clearing}
  onconfirm={() => void clearLog()}
/>

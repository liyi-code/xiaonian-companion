// QuestSystem.cs
// 任务系统的 UI 与状态聚合。Python 端（quest.py）通过 quest_update 事件下发任务状态，
// 这里负责展示 + 在玩家完成目标时回发 quest_event（进度判定在 Python 端）。
//
// 用法：场景里放一个 Canvas + 一个 Text（或 TextMeshPro）做任务面板，挂本脚本并填 questText。
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEngine;
using TMPro;

public class QuestSystem : MonoBehaviour
{
    public static QuestSystem Instance { get; private set; }

    [Header("任务面板 UI")]
    public TextMeshProUGUI questText;     // 显示所有 NPC 当前任务与进度

    // npcId -> (questId -> QuestView)
    private readonly Dictionary<string, Dictionary<string, QuestView>> _board =
        new Dictionary<string, Dictionary<string, QuestView>>();

    void Awake() { Instance = this; }

    // Python 下发任务状态（NpcAgent 转发到这里）
    public void OnQuestUpdate(string npcId, JObject ev)
    {
        if (!_board.TryGetValue(npcId, out var map))
            _board[npcId] = map = new Dictionary<string, QuestView>();

        string qid = (string)ev["quest_id"];
        var q = new QuestView
        {
            title = (string)ev["title"],
            desc = (string)ev["desc"],
            reward = (string)ev["reward"],
            state = (string)ev["state"],
        };
        var objs = ev["objectives"] as JArray;
        if (objs != null)
            foreach (var o in objs)
                q.objectives.Add(new ObjectiveView { text = (string)o["text"], done = (bool)o["done"] });
        map[qid] = q;

        Debug.Log($"[Quest] {npcId} 任务 {q.title} -> {q.state}");
        RefreshUI();
    }

    // 玩家完成某目标时调用（TaskTrigger / NpcAgent 交互回调）
    public void ReportProgress(string npcId, string kind, string objectId = null, string npcFrom = null)
    {
        BridgeHub.Instance?.SendQuestEvent(npcId, kind, objectId, npcFrom);
    }

    private void RefreshUI()
    {
        if (questText == null) return;
        var sb = new System.Text.StringBuilder();
        foreach (var kv in _board)
        {
            sb.AppendLine($"◆ {kv.Key}");
            foreach (var q in kv.Value.Values)
            {
                string mark = q.state == "completed" ? "✔" : (q.state == "rewarded" ? "★" : "○");
                sb.AppendLine($"  {mark} {q.title}");
                foreach (var o in q.objectives)
                    sb.AppendLine($"       {(o.done ? "[x]" : "[ ]")} {o.text}");
            }
        }
        questText.text = sb.ToString();
    }

    private class QuestView
    {
        public string title, desc, reward, state;
        public List<ObjectiveView> objectives = new List<ObjectiveView>();
    }
    private class ObjectiveView { public string text; public bool done; }
}

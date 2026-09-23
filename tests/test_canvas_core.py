import json
import subprocess
from pathlib import Path

CORE_PATH = Path(__file__).parents[1] / "src/image_hub/static/canvas-core.js"


def run_canvas_core(script: str) -> dict:
    # ``encoding`` 必须显式给：Windows 上默认按 GBK 解码，中文断言会变成乱码。
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8")
    return json.loads(completed.stdout)


def test_agent_spec_survives_node_normalization_and_request_snapshot():
    """「Agent 生图器」的轴必须一路带到提交请求里。

    它复用生成节点（同一张卡、同一套结果逻辑），靠 ``agentSpec`` 区分 ——
    归一化或 requestSnapshot 漏了它，提交就没有 agent_spec，服务端按普通生图跑。
    """
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        const agent = core.normalizeGenerationNode({{
          id:'g1', type:'generation_node', x:1, y:2,
          agentSpec: {{platform:'amazon', market:'us', work_type:'replace', selling_point_text:'防滑'}}
        }});
        const plain = core.normalizeGenerationNode({{id:'g2', type:'generation_node'}});
        const snapshot = core.requestSnapshot(agent);
        console.log(JSON.stringify({{
          agent: agent.agentSpec,
          plain: plain.agentSpec,
          snapshot: snapshot.agentSpec,
          plainSnapshot: core.requestSnapshot(plain).agentSpec
        }}));
        """
    )

    assert result["agent"]["platform"] == "amazon"
    assert result["agent"]["selling_point_text"] == "防滑"
    assert result["plain"] is None
    assert result["snapshot"]["work_type"] == "replace"
    assert result["plainSnapshot"] is None


def test_agent_roles_survive_normalization_and_allow_blank():
    """逐张职责要能一路带到提交，且**留空是合法默认**（用户口径：留空由服务端补）。"""
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        const agent = core.normalizeGenerationNode({{
          id:'g1', type:'generation_node',
          agentSpec: {{platform:'amazon'}},
          agentRoles: {{'img-1':'product'}},
        }});
        const plain = core.normalizeGenerationNode({{id:'g2', type:'generation_node'}});
        console.log(JSON.stringify({{
          roles: agent.agentRoles,
          blankRoles: plain.agentRoles,
          specHasRoles: Object.keys(core.requestSnapshot(agent).agentSpec).includes('agentRoles'),
        }}));
        """
    )

    assert result["roles"] == {"img-1": "product"}
    assert result["blankRoles"] == {}
    # agentRoles 刻意不进请求快照：提交时由 generate() 排成 reference_roles 放进 spec。
    assert result["specHasRoles"] is False


def test_agent_card_is_wider_than_the_plain_generator():
    """Agent 生图器多一组轴选择器，默认宽度必须更宽（用户要求「加宽 agent 生图器」）。"""
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        console.log(JSON.stringify({{plain: core.DEFAULT_WIDTH, agent: core.AGENT_DEFAULT_WIDTH}}));
        """
    )
    assert result["agent"] > result["plain"], result


def test_new_request_chooses_first_enabled_profile_and_never_falls_back_for_history():

    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        const profiles = [
          {{id:'libtv:a', enabled:false}},
          {{id:'lovart:b', enabled:true}},
          {{id:'api:c', enabled:true}}
        ];
        console.log(JSON.stringify({{
          selected: core.chooseRequestProfile(profiles, '')?.id || null,
          none: core.chooseRequestProfile(profiles.map(p => ({{...p, enabled:false}})), ''),
          missingHistorical: core.chooseRequestProfile(profiles, 'api:removed'),
          unavailable: core.requestAvailability(null),
          available: core.requestAvailability(profiles[1])
        }}));
        """
    )

    assert result["selected"] == "lovart:b"
    assert result["none"] is None
    assert result["missingHistorical"] is None
    assert result["available"] == {"enabled": True, "message": ""}
    assert result["unavailable"]["enabled"] is False
    assert "不可用" in result["unavailable"]["message"]


def test_history_locate_finds_the_unified_node_and_continue_copies_the_full_request():
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        let sequence = 0;
        const uid = prefix => `${{prefix}}-${{++sequence}}`;
        const nodes = [];
        const item = {{
          id:'generation-old', prompt:'historical prompt', provider:'api',
          model_label:'Legacy model', profile_id:'api:opaque', profile_available:true,
          parameters:{{ratio:'3:4', resolution:'4K', quality:'standard'}},
          parent_generation_id:'earlier-parent', status:'succeeded',
          artifact_url:'/artifact', sentiment:'satisfied', can_retry:false
        }};
        const first = core.historyAction(nodes, item, 'locate', uid);
        const second = core.historyAction(nodes, item, 'locate', uid);
        const continued = core.historyAction(nodes, item, 'continue', uid);
        console.log(JSON.stringify({{
          nodeCount: nodes.length,
          nodeType: nodes[0].type,
          sameNode: first.node.id === second.node.id && first.node.id === continued.node.id,
          locateCreated: first.createdNode,
          secondCreated: second.createdNode,
          focusId: second.focusId,
          resultRef: second.resultRef,
          generationId: first.result.generationId,
          sentiment: first.result.sentiment,
          expanded: nodes[0].expanded,
          create: continued.create
        }}));
        """
    )

    assert result["nodeCount"] == 1
    assert result["nodeType"] == "generation_node"
    assert result["sameNode"] is True
    assert result["locateCreated"] is True
    assert result["secondCreated"] is False
    assert result["focusId"] == result["resultRef"].split(":")[1]
    assert result["generationId"] == "generation-old"
    assert result["sentiment"] == "satisfied"
    assert result["expanded"] is False
    assert result["create"] == {
        "x": 644,
        "y": 150,
        "prompt": "historical prompt",
        "provider": "api",
        "profileId": "api:opaque",
        "ratio": "3:4",
        "resolution": "4K",
        "quality": "standard",
        "count": 1,
        "orderedInputIds": [result["resultRef"]],
        "parentGenerationId": "generation-old",
        "expanded": True,
    }


def test_migrates_legacy_canvas_once_splits_results_and_remaps_references():
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        const legacy = [
          {{id:'upload-1', type:'image', x:42, y:73, width:280}},
          {{id:'request-1', type:'generation_request', x:500, y:120, width:344,
            prompt:'legacy draft', profileId:'api:opaque', ratio:'3:4', resolution:'2K',
            quality:'standard', count:2, expanded:true, orderedInputIds:['upload-1','result-2']}},
          {{id:'result-1', type:'generation_result', requestId:'request-1', generationId:'evidence-1',
            artifactUrl:'/generations/evidence-1/artifact', status:'succeeded', sentiment:'adopted',
            aspect:'3/4', parameters:{{ratio:'3:4', resolution:'2K'}}}},
          {{id:'result-2', type:'generation_result', requestId:'request-1', generationId:'evidence-2',
            artifactUrl:'/generations/evidence-2/artifact', status:'succeeded', aspect:'3/4'}},
          {{id:'result-3', type:'generation_result', generationId:'evidence-3',
            artifactUrl:'/generations/evidence-3/artifact', status:'succeeded', aspect:'1/1'}}
        ];
        const first = core.migrateCanvasNodes(legacy);
        const second = core.migrateCanvasNodes(first.nodes);
        const merged = first.nodes.find(node => node.id === 'request-1');
        const split = first.nodes.find(node => node.id === 'request-1-result-2');
        const orphan = first.nodes.find(node => node.id === 'generation-result-3');
        console.log(JSON.stringify({{
          migrated: first.migrated,
          secondMigrated: second.migrated,
          nodeCount: first.nodes.length,
          types: first.nodes.map(node => node.type),
          inputRefs: merged.orderedInputIds,
          resultIds: merged.activeBatch.results.map(item => item.id),
          resultGenerationIds: merged.activeBatch.results.map(item => item.generationId),
          sentiment: merged.activeBatch.results.map(item => item.sentiment),
          primaryResultId: merged.primaryResultId,
          expanded: merged.expanded,
          draftPrompt: merged.prompt,
          batchRequest: merged.activeBatch.request,
          splitCount: split.count,
          splitX: split.x,
          splitY: split.y,
          splitExpanded: split.expanded,
          splitInputRefs: split.orderedInputIds,
          splitResultIds: split.activeBatch.results.map(item => item.id),
          splitGenerationIds: split.activeBatch.results.map(item => item.generationId),
          splitPrimaryResultId: split.primaryResultId,
          splitPrompt: split.prompt,
          splitRatio: split.ratio,
          everyCardHasOneResult: first.nodes
            .filter(node => node.type === 'generation_node')
            .every(node => core.batchResults(node.activeBatch).length === 1),
          orphanType: orphan.type,
          orphanGenerationId: orphan.activeBatch.results[0].generationId,
          stableSecondPass: JSON.stringify(second.nodes) === JSON.stringify(first.nodes)
        }}));
        """
    )

    assert result["migrated"] is True
    assert result["secondMigrated"] is False
    assert result["stableSecondPass"] is True
    assert result["nodeCount"] == 4
    assert result["types"] == [
        "image",
        "generation_node",
        "generation_node",
        "generation_node",
    ]
    assert result["inputRefs"] == ["upload-1", "result:request-1-result-2:result-2"]
    assert result["resultIds"] == ["result-1"]
    assert result["resultGenerationIds"] == ["evidence-1"]
    assert result["sentiment"] == ["adopted"]
    assert result["primaryResultId"] == "result-1"
    assert result["expanded"] is False
    assert result["draftPrompt"] == "legacy draft"
    # The submitting card keeps result #1 and now reports the single-result recipe.
    assert result["batchRequest"]["count"] == 1
    assert result["batchRequest"]["orderedInputIds"] == [
        "upload-1",
        "result:request-1-result-2:result-2",
    ]
    # Result #2 becomes its own sibling card, right of the origin, with the recipe.
    assert result["splitCount"] == 1
    assert (result["splitX"], result["splitY"]) == (964, 120)
    assert result["splitExpanded"] is False
    assert result["splitResultIds"] == ["result-2"]
    assert result["splitGenerationIds"] == ["evidence-2"]
    assert result["splitPrimaryResultId"] == "result-2"
    assert result["splitPrompt"] == "legacy draft"
    assert result["splitRatio"] == "3:4"
    assert result["splitInputRefs"] == ["upload-1", "result:request-1-result-2:result-2"]
    assert result["everyCardHasOneResult"] is True
    assert result["orphanType"] == "generation_node"
    assert result["orphanGenerationId"] == "evidence-3"


def test_restored_local_upload_requires_reselection_without_changing_relationships():
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        const restored = core.restoreCanvasNodes([
          {{id:'upload-1', type:'image', x:42, y:73, src:'blob:old', localOnly:true}},
          {{id:'generation-1', type:'generation_node', orderedInputIds:['upload-1']}}
        ]);
        const missing = core.firstMissingLocalInput(restored, restored[1], () => false);
        const satisfied = core.firstMissingLocalInput(restored, restored[1], id => id === 'upload-1');
        console.log(JSON.stringify({{
          restored,
          missingId: missing?.id || null,
          satisfied: satisfied
        }}));
        """
    )

    restored = result["restored"]
    assert restored[0]["x"] == 42
    assert restored[0]["y"] == 73
    assert restored[0]["src"] == ""
    assert restored[0]["needsReselect"] is True
    assert restored[1]["type"] == "generation_node"
    assert restored[1]["orderedInputIds"] == ["upload-1"]
    assert result["missingId"] == "upload-1"
    assert result["satisfied"] is None


def test_copy_paste_carries_the_recipe_without_fabricating_generation_evidence():
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        let sequence = 0;
        const uid = prefix => `${{prefix}}-new-${{++sequence}}`;
        const nodes = [
          {{id:'image-a', type:'image', x:10, y:20, width:200}},
          {{id:'generation-a', type:'generation_node', x:250, y:20, width:344, submitting:true, dirty:true,
            orderedInputIds:['image-a','missing','result:generation-a:result-a','result:generation-b:result-b'],
            primaryResultId:'result-a',
            activeBatch:{{id:'batch-a', request:{{prompt:'kept', profileId:'api:opaque', ratio:'3:4', resolution:'2K', quality:'standard', count:1, orderedInputIds:['image-a']}},
              results:[{{id:'result-a', generationId:'evidence-a', artifactUrl:'/a', status:'succeeded', sentiment:'adopted', canRetry:true}}]}}}},
          {{id:'generation-b', type:'generation_node', x:650, y:20, width:344,
            activeBatch:{{id:'batch-b', request:{{prompt:'other'}},
              results:[{{id:'result-b', generationId:'evidence-b', artifactUrl:'/b', status:'succeeded'}}]}}}}
        ];
        const copied = core.clonePresentationNodes(nodes, ['image-a','generation-a'], uid, {{x:36,y:40}});
        console.log(JSON.stringify(copied));
        """
    )
    clones = {node["type"]: node for node in result["clones"]}
    assert clones["image"]["x"] == 46
    assert clones["image"]["y"] == 60
    assert clones["generation_node"]["orderedInputIds"] == [clones["image"]["id"], "missing", "result:generation-b:result-b"]
    assert clones["generation_node"]["submitting"] is False
    assert clones["generation_node"]["dirty"] is False
    assert clones["generation_node"]["activeBatch"] is None
    assert clones["generation_node"]["attempt"] is None
    assert clones["generation_node"]["primaryResultId"] == ""
    assert clones["generation_node"]["expanded"] is True
    assert clones["generation_node"]["prompt"] == "kept"
    assert "result-a" not in json.dumps(clones)


def test_delete_is_presentation_only_and_prunes_references():
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        const nodes = [
          {{id:'upload-1', type:'image'}},
          {{id:'generation-1', type:'generation_node',
            orderedInputIds:['upload-1','result:generation-2:result-b']}},
          {{id:'generation-2', type:'generation_node',
            activeBatch:{{id:'batch-b', request:{{prompt:'keep-in-database'}},
              results:[{{id:'result-b', generationId:'keep-in-database', artifactUrl:'/b', status:'succeeded'}}]}}}}
        ];
        console.log(JSON.stringify(core.removePresentationNodes(nodes, ['upload-1','generation-2'])));
        """
    )
    assert result == [{
        "id": "generation-1",
        "type": "generation_node",
        "orderedInputIds": [],
    }]


def test_geometry_converts_screen_centers_once_and_builds_minimap():
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        const port = core.rectCenterToWorld(
          {{left:510,top:305,width:12,height:12}},
          {{left:100,top:50,width:800,height:600}},
          {{x:80,y:35,zoom:1.5}}
        );
        const visible = core.visibleWorld({{width:800,height:600}}, {{x:80,y:35,zoom:1.5}});
        const mini = core.minimapGeometry([{{x:10,y:20,width:200,height:100}}], visible);
        const center = core.minimapPointToWorld({{
          x:mini.viewport.x + mini.viewport.width/2,
          y:mini.viewport.y + mini.viewport.height/2
        }}, mini);
        console.log(JSON.stringify({{port,visible,mini,center}}));
        """
    )
    assert result["port"] == {"x": 224, "y": 150.66666666666666}
    assert result["visible"]["x"] == -53.333333333333336
    assert result["visible"]["width"] == 533.3333333333334
    assert len(result["mini"]["nodes"]) == 1
    assert abs(result["center"]["x"] - (result["visible"]["x"] + result["visible"]["width"] / 2)) < 1e-9
    assert abs(result["center"]["y"] - (result["visible"]["y"] + result["visible"]["height"] / 2)) < 1e-9


def test_editable_targets_disable_canvas_shortcuts():
    result = run_canvas_core(
        f"""
        const core = require({json.dumps(str(CORE_PATH))});
        console.log(JSON.stringify({{
          input: core.isEditableTarget({{tagName:'INPUT'}}),
          canvas: core.isEditableTarget({{tagName:'SECTION', isContentEditable:false}}),
          editable: core.isEditableTarget({{tagName:'DIV', isContentEditable:true}})
        }}));
        """
    )
    assert result == {"input": True, "canvas": False, "editable": True}

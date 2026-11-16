<script setup lang="ts">
import {computed} from 'vue';
import {AlertTriangle} from 'lucide-vue-next';
import {useResearch} from '../stores/research';
import {ReplacementState,RunStatus} from '../constants/enums';
import {time} from '../utils/format';
import DataTable from './common/DataTable.vue';

const s=useResearch();
const summary=computed(()=>s.impact?.summary);
const rows=computed(()=>s.impact?.samples||[]);
const affectedRuns=computed(()=>{
  const seen=new Map<number,{id:number;name:string;status:string;publishable:boolean;replacementRunId:number|null;replacementState:string;replacementError:string;samples:string[]}>();
  for(const row of rows.value){
    for(const run of row.affected_runs){
      const entry=seen.get(run.id)||{id:run.id,name:run.name,status:run.status,publishable:run.publishable,replacementRunId:run.replacement_run_id,replacementState:run.replacement_state,replacementError:run.replacement_error,samples:[]};
      entry.samples.push(row.code);
      seen.set(run.id,entry);
    }
  }
  return [...seen.values()];
});
</script>

<template>
  <section v-if="summary&&(summary.revoked_sources>0||summary.affected_runs>0)" class="impact-panel">
    <div class="impact-head">
      <div><span class="eyebrow">CONTAMINATION IMPACT / 来源撤销影响</span><h2>已撤销测谱的影响范围</h2></div>
      <span class="warning"><AlertTriangle :size="14"/> 旧结果保留查阅，停止发布与导出</span>
    </div>
    <template v-if="summary"><div class="impact-stats">
      <div><span>撤销来源</span><b>{{summary.revoked_sources}}</b></div>
      <div><span>受影响研究</span><b>{{summary.affected_runs}}</b></div>
      <div><span>替代进行中</span><b>{{summary.replacements_pending+summary.replacements_running}}</b></div>
      <div><span>替代完成</span><b>{{summary.replacements_completed}}</b></div>
      <div><span>替代受阻/失败</span><b class="warning">{{summary.replacements_blocked+summary.replacements_failed}}</b></div>
    </div></template>
    <DataTable :head="['受影响研究','原状态','发布','替代研究','替代进度','失败/受阻原因']">
      <tr v-for="run in affectedRuns" :key="run.id">
        <td><RouterLink class="text-link" :to="'/results?run='+run.id">#{{run.id}} {{run.name}}</RouterLink><small>涉及 {{run.samples.join('、')}}</small></td>
        <td>{{RunStatus[run.status as keyof typeof RunStatus]||run.status}}</td>
        <td><span :class="run.publishable?'supported':'warning'">{{run.publishable?'可发布':'已停止'}}</span></td>
        <td>{{run.replacementRunId?('#'+run.replacementRunId):'待生成'}}</td>
        <td><span :class="run.replacementState==='completed'?'supported':run.replacementState==='failed'||run.replacementState==='blocked'?'warning':''">{{ReplacementState[run.replacementState as keyof typeof ReplacementState]||run.replacementState}}</span></td>
        <td class="note-cell">{{run.replacementError||'—'}}</td>
      </tr>
    </DataTable>
    <p v-if="!affectedRuns.length" class="caption">撤销已登记，尚未回溯到关联研究；研究补齐来源关系后会同样受控。</p>
  </section>
</template>

<script setup lang="ts">
import {computed,ref} from 'vue';
import {AlertTriangle,RefreshCw,ShieldCheck,Upload} from 'lucide-vue-next';
import {Sample,SpectrumSource} from '../types/domain';
import {useAuth} from '../stores/auth';
import {useAction} from '../hooks/useAction';
import {registerReplacement,revokeSource} from '../api/sources';
import {time} from '../utils/format';
import DataTable from './common/DataTable.vue';

const props=defineProps<{sample:Sample}>();
const emit=defineEmits<{changed:[]}>();
const auth=useAuth(),a=useAction();
const revokeReason=ref(''),replacementReason=ref(''),replacementValues=ref('');
const sources=computed(()=>props.sample.sources||[]);
const revoked=computed(()=>sources.value.filter(s=>s.status==='revoked'));
const active=computed(()=>sources.value.find(s=>s.status==='active')||null);
const canWrite=computed(()=>auth.user?.role!=='viewer');
const needsReplacement=computed(()=>!active.value&&revoked.value.length>0);

function doRevoke(source:SpectrumSource){
  a.execute(async()=>{
    await revokeSource(props.sample.id,source.id,revokeReason.value);
    revokeReason.value='';
    emit('changed');
  })
}
function doRegister(){
  a.execute(async()=>{
    const values=replacementValues.value.split(/[\s,，]+/).filter(Boolean).map(Number);
    await registerReplacement(props.sample.id,values,replacementReason.value);
    replacementValues.value='';replacementReason.value='';
    emit('changed');
  })
}
function prefill(){
  const base=(active.value||revoked.value[revoked.value.length-1]);
  replacementValues.value=base?base.observed.map(v=>v.toFixed(6)).join(' '):'';
}
</script>

<template>
  <section class="source-panel">
    <div class="source-head">
      <div>
        <span class="eyebrow">{{sample.code}} / SOURCE LINEAGE</span>
        <h3>{{active?'当前有效测谱':'测谱已撤销，等待替代谱'}}</h3>
      </div>
      <span v-if="active" class="supported">有效 · {{active.code}}</span>
      <span v-else class="warning">撤销 {{revoked.length}} 份 / 无可用谱</span>
    </div>
    <DataTable :head="['测谱来源','类型','状态','登记人','时间','说明']">
      <tr v-for="s in sources" :key="s.id">
        <td class="mono">{{s.code}}</td>
        <td>{{s.kind==='replacement'?'替代谱':'实测谱'}}</td>
        <td><span :class="s.status==='active'?'supported':'warning'">{{s.status==='active'?'有效':'已撤销'}}</span></td>
        <td>{{s.owner||'—'}}</td>
        <td class="mono">{{time(s.created_at)}}</td>
        <td class="note-cell">{{s.reason||'—'}}</td>
      </tr>
    </DataTable>
    <div v-if="canWrite&&active" class="source-form">
      <label>撤销原因<textarea aria-label="撤销原因" v-model="revokeReason" minlength="8" maxlength="1000" placeholder="至少8字，例如：质检发现该测谱受交叉污染，需重新测谱。"/></label>
      <button class="outline" :disabled="a.busy.value||revokeReason.length<8" @click="doRevoke(active)"><AlertTriangle :size="14"/>撤销当前测谱</button>
      <p class="caption">撤销不删除原谱与旧结果；原研究保留查阅，但停止发布与导出，并自动生成替代研究。</p>
    </div>
    <div v-if="canWrite&&needsReplacement" class="source-form">
      <div class="section-heading"><h2>登记替代谱</h2><span>{{sample.observed.length}} 个波段 · 反射率</span></div>
      <label>替代谱数值<input aria-label="替代谱数值" v-model="replacementValues" placeholder="以空格或逗号分隔各波段反射率"/></label>
      <label>替代原因<textarea aria-label="替代原因" v-model="replacementReason" minlength="8" maxlength="1000" placeholder="至少8字，例如：重新称量测谱并复核端元校准。"/></label>
      <div class="source-actions">
        <button class="outline" type="button" @click="prefill"><RefreshCw :size="14"/>从旧谱填充</button>
        <button class="primary" :disabled="a.busy.value||replacementReason.length<8||!replacementValues.trim()" @click="doRegister"><Upload :size="14"/>登记替代谱并重算</button>
      </div>
    </div>
    <p v-else-if="canWrite&&active" class="caption"><ShieldCheck :size="13"/> 登记替代谱前必须先撤销受污染的测谱。</p>
    <p v-if="a.error.value" role="alert" class="notice">{{a.error.value}}</p>
    <p class="caption">旧谱数值仅供查阅与追溯，不会随撤销移除：{{revoked.length?revoked.map(s=>s.code).join('、'):'无撤销记录'}}。</p>
  </section>
</template>

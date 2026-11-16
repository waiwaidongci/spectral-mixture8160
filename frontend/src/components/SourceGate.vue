<script setup lang="ts">
import {computed} from 'vue';
import {ShieldAlert,RefreshCw} from 'lucide-vue-next';
import {Run} from '../types/domain';
import {useAuth} from '../stores/auth';
import {useAction} from '../hooks/useAction';
import {retryReplacement} from '../api/sources';
import {ReplacementState,RunStatus} from '../constants/enums';

const props=defineProps<{run:Run}>();
const emit=defineEmits<{changed:[]}>();
const auth=useAuth(),a=useAction();
const blocked=computed(()=>!props.run.publishable&&(props.run.publish_state==='revoked'||props.run.status==='blocked'||props.run.source_links.some(l=>l.source_status==='revoked')));
const retryable=computed(()=>props.run.replacement_state==='failed'||props.run.replacement_state==='blocked');
function retry(){
  a.execute(async()=>{await retryReplacement(props.run.id);emit('changed')})
}
</script>

<template>
  <section v-if="blocked||run.is_replacement" :class="['gate-banner',blocked?'blocked':'replacement']">
    <div class="gate-icon"><ShieldAlert :size="19"/></div>
    <div>
      <b v-if="run.is_replacement">替代研究 · 原研究 #{{run.replacement_of_run_id}}</b>
      <b v-else>来源测谱已撤销，本结果停止发布与导出</b>
      <p v-if="blocked">{{run.blocked_reason||'受影响样本的来源测谱已撤销。'}}</p>
      <p v-else>按原参数生成；仅重算受影响样本 {{run.replacement_sample_ids.length}} 个，其余逐次配比原样复用。{{run.result?.reuse_note||''}}</p>
      <p v-if="run.replacement_error" class="warning">替代失败原因：{{run.replacement_error}}</p>
      <p v-else-if="!run.is_replacement">
        替代进度：{{ReplacementState[run.replacement_state as keyof typeof ReplacementState]||run.replacement_state}}
        <template v-if="run.replacement_run_id"> · <RouterLink class="text-link" :to="'/results?run='+run.replacement_run_id">查看替代研究 #{{run.replacement_run_id}}</RouterLink></template>
      </p>
      <p v-if="blocked" class="caption">原谱、原始结果与研究记录均保留可查；审批与导出已停止。</p>
      <p v-if="run.result_history?.length" class="caption">本替代研究曾重算 {{run.result_history.length}} 次，旧指纹仍保留供追溯：<span class="mono">{{run.result_history.map(x=>x.input_hash.slice(0,12)).join('、')}}</span></p>
    </div>
    <button v-if="retryable&&auth.user?.role!=='viewer'" class="outline" :disabled="a.busy.value" @click="retry"><RefreshCw :size="14"/>重试替代</button>
  </section>
  <p v-if="a.error.value" role="alert" class="notice">{{a.error.value}}</p>
</template>

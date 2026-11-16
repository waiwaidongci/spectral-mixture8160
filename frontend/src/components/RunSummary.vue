<script setup lang="ts">
import {Run} from '../types/domain';
import {scientific} from '../utils/format';
import {ReplacementState} from '../constants/enums';
defineProps<{run:Run}>();
</script>
<template>
  <div class="run-summary">
    <div>
      <span class="eyebrow">{{run.result.library.code}} / FIXED ENDMEMBERS <template v-if="run.is_replacement"> / REPLACEMENT</template></span>
      <h3>{{run.result.library.name}}</h3>
      <p>{{run.result.samples.length}} 个混合样本 · {{run.result.endmembers.length}} 个端元 · {{run.result.library.wavelengths.length}} 波段</p>
      <p v-if="run.is_replacement">替代研究：仅重算 {{run.replacement_sample_ids.length}} 个受影响样本，其余原样复用。</p>
      <p v-else-if="run.replacement_run_id">替代进度：{{ReplacementState[run.replacement_state as keyof typeof ReplacementState]||run.replacement_state}} · 替代研究 #{{run.replacement_run_id}}</p>
    </div>
    <div class="summary-number"><span>增广条件数</span><b>{{scientific(run.result.diagnostics.condition_number)}}</b><small>满列秩 / 唯一解释先决</small></div>
    <div class="summary-number"><span>条件扰动</span><b>{{run.result.repeats}} <i>次</i></b><small>σ {{run.result.noise_sigma}} / 固定端元</small></div>
  </div>
</template>

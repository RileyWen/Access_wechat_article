<script setup lang="ts">
import { Chart } from '@antv/g2'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ArchiveDistributionDatum } from '../utils/archiveDistribution'

const props = defineProps<{
  data: ArchiveDistributionDatum[]
  total: number
}>()

const chartContainerRef = ref<HTMLDivElement | null>(null)
let chart: Chart | null = null

function destroyChart() {
  if (!chart) {
    return
  }
  chart.destroy()
  chart = null
}

async function renderChart() {
  await nextTick()
  if (!chartContainerRef.value || props.data.length === 0) {
    destroyChart()
    return
  }

  destroyChart()
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  chart = new Chart({
    container: chartContainerRef.value,
    autoFit: true,
    height: 176,
  })
  chart.options({
    type: 'interval',
    data: props.data,
    coordinate: { type: 'theta', innerRadius: 0.68, outerRadius: 0.92 },
    transform: [{ type: 'stackY' }],
    encode: { y: 'value', color: 'name' },
    scale: {
      color: {
        domain: props.data.map((item) => item.name),
        range: props.data.map((item) => item.color),
      },
    },
    legend: false,
    axis: false,
    style: {
      stroke: 'rgba(248, 251, 255, 0.96)',
      lineWidth: 2,
    },
    animate: reduceMotion ? false : {
      enter: { type: 'waveIn', duration: 280 },
      update: { duration: 180 },
    },
  })
  await chart.render()
}

onMounted(() => {
  void renderChart()
})

watch(
  () => props.data,
  () => {
    void renderChart()
  },
  { deep: true },
)

onBeforeUnmount(() => {
  destroyChart()
})
</script>

<template>
  <div
    class="archive-distribution-chart"
    role="img"
    :aria-label="`公众号文章数量分布，共 ${total} 篇文章`"
  >
    <div ref="chartContainerRef" class="archive-distribution-canvas" aria-hidden="true"></div>
    <div class="archive-distribution-center" aria-hidden="true">
      <strong>{{ total }}</strong>
      <span>篇文章</span>
    </div>
  </div>
</template>

<style scoped>
.archive-distribution-chart {
  position: relative;
  width: 100%;
  min-width: 0;
  height: 176px;
}

.archive-distribution-canvas {
  width: 100%;
  height: 176px;
}

.archive-distribution-center {
  position: absolute;
  top: 50%;
  left: 50%;
  display: grid;
  justify-items: center;
  gap: 2px;
  color: #15386f;
  pointer-events: none;
  transform: translate(-50%, -50%);
}

.archive-distribution-center strong {
  font-size: 24px;
  font-weight: 500;
  line-height: 1;
}

.archive-distribution-center span {
  color: rgba(21, 56, 111, 0.72);
  font-size: 12px;
  font-weight: 400;
}

:global(.collector-app.dark) .archive-distribution-center {
  color: #dce7f5;
}

:global(.collector-app.dark) .archive-distribution-center span {
  color: #9fb2cc;
}
</style>

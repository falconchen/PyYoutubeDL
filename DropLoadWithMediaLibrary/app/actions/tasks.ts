'use server'

import { auth } from '@/lib/auth'
import { db } from '@/lib/db'
import { downloadTasks } from '@/lib/db/schema'
import { desc, eq } from 'drizzle-orm'
import { headers } from 'next/headers'
import { revalidatePath } from 'next/cache'

async function getUserId() {
  const session = await auth.api.getSession({ headers: await headers() })
  if (!session?.user) throw new Error('Unauthorized')
  return session.user.id
}

export async function getTasks() {
  const userId = await getUserId()
  return db.select().from(downloadTasks).where(eq(downloadTasks.userId, userId)).orderBy(desc(downloadTasks.createdAt))
}

export async function createTask(url: string, formats: string[]) {
  const userId = await getUserId()
  if (!url.trim() || formats.length === 0) throw new Error('Invalid task')
  await db.insert(downloadTasks).values({ userId, url: url.trim(), formats: formats.join(','), status: 'queued' })
  revalidatePath('/')
}
